"""Private SQLite state for channel connections and their audit facts.

Credentials are write-only from the API's perspective: they live in this
0600 database under the host data root and are loaded only to construct an
adapter. Configuration rows and delivery audit rows deliberately have no
foreign key relationship, so deleting a connection keeps its delivery history.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cairn.channels.models import (
    ChannelConnection,
    ChannelDesiredState,
    ChannelInboundEvent,
    ChannelNotFound,
    ChannelOperationUncertain,
    ChannelPlatform,
    ChannelRequestConflict,
    ChannelRevisionConflict,
    ChannelSendStatus,
    new_connection_id,
    utc_now,
    validate_allowed_senders,
    validate_credentials,
    validate_metadata,
)

SCHEMA_VERSION = 2
MAX_REQUESTS = 512


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _payload_digest(target: str, message: str) -> str:
    return hashlib.sha256(f"{target}\0{message}".encode()).hexdigest()


def inbound_digest(event: ChannelInboundEvent) -> str:
    """Stable content digest used to detect an external message ID collision.

    ``received_at`` is transport metadata and is deliberately excluded: a
    delivery retry may carry a new receipt time but is still the same message.
    """
    return _digest(
        {
            "platform": event.platform.value,
            "connection_id": event.connection_id,
            "external_message_id": event.external_message_id,
            "sender_id": event.sender_id,
            "conversation_binding": event.conversation_binding,
            "text": event.text,
        }
    )


@dataclass(frozen=True, slots=True)
class CreateResult:
    connection: ChannelConnection
    replayed: bool


@dataclass(frozen=True, slots=True)
class UpdateResult:
    connection: ChannelConnection
    replayed: bool
    credentials_replaced: bool


@dataclass(frozen=True, slots=True)
class DeleteResult:
    connection_id: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class RequestClaim:
    replayed: bool
    result: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class InboundClaim:
    created: bool
    state: str
    event_digest: str
    conversation_id: str | None
    run_id: str | None
    error: str | None


@dataclass(frozen=True, slots=True)
class DeliveryRecord:
    delivery_id: str
    connection_id: str
    platform: str
    idempotency_key: str
    target_digest: str
    status: str
    error: str | None
    created_at: str
    updated_at: str


class ChannelStore:
    """One private database; every mutation is a local transaction."""

    def __init__(self, root: Path) -> None:
        raw = root.expanduser()
        if raw.is_symlink():
            raise ValueError("Channel state cannot be a symlink")
        self.root = raw.resolve()
        if self.root.is_symlink():
            raise ValueError("Channel state cannot be a symlink")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with suppress(OSError):
            self.root.chmod(0o700)
        self.path = self.root / "channels.sqlite3"
        if self.path.is_symlink():
            raise ValueError("Channel store cannot be a symlink")
        with self._connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version not in (0, 1, SCHEMA_VERSION):
                raise ValueError("Unsupported channel store schema")
            self._create_schema(connection)
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(channel_connections)"
                ).fetchall()
            }
            if "allowed_senders_json" not in columns:
                connection.execute(
                    "ALTER TABLE channel_connections ADD COLUMN "
                    "allowed_senders_json TEXT NOT NULL DEFAULT '[]'"
                )
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        with suppress(OSError):
            self.path.chmod(0o600)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS channel_connections (
                connection_id TEXT PRIMARY KEY,
                platform TEXT NOT NULL,
                display_name TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                desired_state TEXT NOT NULL,
                credential_ref TEXT,
                owner_identity TEXT,
                allowed_senders_json TEXT NOT NULL DEFAULT '[]',
                metadata_json TEXT NOT NULL,
                revision INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS channel_credentials (
                credential_ref TEXT PRIMARY KEY,
                secret_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS channel_requests (
                request_id TEXT PRIMARY KEY,
                operation TEXT NOT NULL,
                digest TEXT NOT NULL,
                state TEXT NOT NULL,
                result_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS channel_inbound (
                platform TEXT NOT NULL,
                connection_id TEXT NOT NULL,
                external_message_id TEXT NOT NULL,
                event_digest TEXT NOT NULL,
                state TEXT NOT NULL,
                conversation_id TEXT,
                run_id TEXT,
                error TEXT,
                received_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (connection_id, external_message_id)
            );
            CREATE TABLE IF NOT EXISTS channel_deliveries (
                delivery_id TEXT PRIMARY KEY,
                connection_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                target_digest TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (connection_id, idempotency_key)
            );
            """
        )

    @staticmethod
    def _connection(row: sqlite3.Row) -> ChannelConnection:
        return ChannelConnection(
            connection_id=str(row["connection_id"]),
            platform=ChannelPlatform(str(row["platform"])),
            display_name=str(row["display_name"]),
            enabled=bool(row["enabled"]),
            desired_state=ChannelDesiredState(str(row["desired_state"])),
            credential_ref=(
                str(row["credential_ref"])
                if row["credential_ref"] is not None
                else None
            ),
            owner_identity=(
                str(row["owner_identity"])
                if row["owner_identity"] is not None
                else None
            ),
            allowed_senders=tuple(json.loads(str(row["allowed_senders_json"]))),
            metadata=json.loads(str(row["metadata_json"])),
            revision=int(row["revision"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _delivery(row: sqlite3.Row) -> DeliveryRecord:
        return DeliveryRecord(
            delivery_id=str(row["delivery_id"]),
            connection_id=str(row["connection_id"]),
            platform=str(row["platform"]),
            idempotency_key=str(row["idempotency_key"]),
            target_digest=str(row["target_digest"]),
            status=str(row["status"]),
            error=str(row["error"]) if row["error"] is not None else None,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def list_connections(
        self, platform: ChannelPlatform | None = None
    ) -> tuple[ChannelConnection, ...]:
        with self._connect() as connection:
            if platform is None:
                rows = connection.execute(
                    "SELECT * FROM channel_connections "
                    "ORDER BY created_at, connection_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM channel_connections WHERE platform = ? "
                    "ORDER BY created_at, connection_id",
                    (platform.value,),
                ).fetchall()
        return tuple(self._connection(row) for row in rows)

    def get_connection(self, connection_id: str) -> ChannelConnection | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM channel_connections WHERE connection_id = ?",
                (connection_id,),
            ).fetchone()
        return None if row is None else self._connection(row)

    @staticmethod
    def _request(
        connection: sqlite3.Connection,
        request_id: str,
        operation: str,
        digest: str,
    ) -> RequestClaim | None:
        row = connection.execute(
            "SELECT * FROM channel_requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        if row is None:
            return None
        if str(row["operation"]) != operation or str(row["digest"]) != digest:
            raise ChannelRequestConflict("request_id was reused for different input")
        if str(row["state"]) == "pending":
            raise ChannelOperationUncertain(
                "previous channel operation has an unknown outcome"
            )
        result = json.loads(str(row["result_json"])) if row["result_json"] else {}
        if not isinstance(result, dict):
            raise RuntimeError("channel request receipt is corrupt")
        return RequestClaim(replayed=True, result=result)

    @staticmethod
    def _start_request(
        connection: sqlite3.Connection,
        request_id: str,
        operation: str,
        digest: str,
        now: str,
    ) -> None:
        connection.execute(
            "INSERT INTO channel_requests "
            "(request_id, operation, digest, state, created_at, updated_at) "
            "VALUES (?, ?, ?, 'pending', ?, ?)",
            (request_id, operation, digest, now, now),
        )

    @staticmethod
    def _finish_request(
        connection: sqlite3.Connection,
        request_id: str,
        result: dict[str, Any],
        now: str,
    ) -> None:
        connection.execute(
            "UPDATE channel_requests SET state = 'completed', result_json = ?, "
            "updated_at = ? WHERE request_id = ?",
            (_json(result), now, request_id),
        )
        count = int(
            connection.execute("SELECT COUNT(*) FROM channel_requests").fetchone()[0]
        )
        if count > MAX_REQUESTS:
            connection.execute(
                "DELETE FROM channel_requests WHERE request_id IN "
                "(SELECT request_id FROM channel_requests "
                "WHERE state = 'completed' ORDER BY updated_at LIMIT ?)",
                (count - MAX_REQUESTS,),
            )

    def create_connection(
        self,
        *,
        platform: ChannelPlatform,
        display_name: str,
        enabled: bool,
        owner_identity: str | None,
        metadata: dict[str, Any],
        credentials: dict[str, str] | None,
        request_id: str,
        allowed_senders: object = (),
    ) -> CreateResult:
        if not request_id.strip():
            raise ValueError("request_id is required")
        name = display_name.strip()
        if not name or len(name) > 120:
            raise ValueError("display_name must be 1-120 characters")
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        owner = None if owner_identity is None else owner_identity.strip()
        if owner is not None and (not owner or len(owner) > 200):
            raise ValueError("owner_identity must be 1-200 characters")
        checked_senders = validate_allowed_senders(allowed_senders)
        checked_metadata = validate_metadata(metadata)
        checked_credentials = (
            None if credentials is None else validate_credentials(credentials)
        )
        now = utc_now()
        digest = _digest(
            {
                "operation": "create",
                "platform": platform.value,
                "display_name": name,
                "enabled": enabled,
                "owner_identity": owner,
                "allowed_senders": checked_senders,
                "metadata": checked_metadata,
                "credentials": (
                    None
                    if checked_credentials is None
                    else _digest(checked_credentials)
                ),
            }
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._request(connection, request_id, "create", digest)
            if replay is not None:
                return CreateResult(
                    connection=ChannelConnection.model_validate(replay.result),
                    replayed=True,
                )
            self._start_request(connection, request_id, "create", digest, now)
            credential_ref = (
                None if checked_credentials is None else "cred-" + secrets.token_hex(16)
            )
            record = ChannelConnection(
                connection_id=new_connection_id(),
                platform=platform,
                display_name=name,
                enabled=enabled,
                desired_state=ChannelDesiredState.STOPPED,
                credential_ref=credential_ref,
                owner_identity=owner,
                allowed_senders=checked_senders,
                metadata=checked_metadata,
                revision=1,
                created_at=now,
                updated_at=now,
            )
            if credential_ref is not None and checked_credentials is not None:
                connection.execute(
                    "INSERT INTO channel_credentials "
                    "(credential_ref, secret_json, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (credential_ref, _json(checked_credentials), now, now),
                )
            connection.execute(
                "INSERT INTO channel_connections "
                "(connection_id, platform, display_name, enabled, desired_state, "
                "credential_ref, owner_identity, allowed_senders_json, metadata_json, "
                "revision, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.connection_id,
                    record.platform.value,
                    record.display_name,
                    int(record.enabled),
                    record.desired_state.value,
                    record.credential_ref,
                    record.owner_identity,
                    _json(list(record.allowed_senders)),
                    _json(record.metadata),
                    record.revision,
                    record.created_at,
                    record.updated_at,
                ),
            )
            self._finish_request(
                connection, request_id, record.model_dump(mode="json"), now
            )
        return CreateResult(connection=record, replayed=False)

    def update_connection(
        self,
        connection_id: str,
        *,
        expected_revision: int,
        request_id: str,
        changes: dict[str, Any],
        credentials: dict[str, str] | None = None,
        replace_credentials: bool = False,
    ) -> UpdateResult:
        allowed = {
            "display_name",
            "enabled",
            "owner_identity",
            "allowed_senders",
            "metadata",
        }
        if set(changes) - allowed or (not changes and not replace_credentials):
            raise ValueError("channel update has no valid changes")
        clean: dict[str, Any] = dict(changes)
        if "display_name" in clean:
            if not isinstance(clean["display_name"], str):
                raise ValueError("display_name must be a string")
            clean["display_name"] = clean["display_name"].strip()
            if not 1 <= len(clean["display_name"]) <= 120:
                raise ValueError("display_name must be 1-120 characters")
        if "enabled" in clean and type(clean["enabled"]) is not bool:
            raise ValueError("enabled must be a boolean")
        if "owner_identity" in clean:
            owner = clean["owner_identity"]
            if owner is not None:
                if not isinstance(owner, str) or not owner.strip():
                    raise ValueError("owner_identity must be a string or null")
                clean["owner_identity"] = owner.strip()
                if len(clean["owner_identity"]) > 200:
                    raise ValueError("owner_identity is too long")
        if "metadata" in clean:
            clean["metadata"] = validate_metadata(clean["metadata"])
        if "allowed_senders" in clean:
            clean["allowed_senders"] = list(
                validate_allowed_senders(clean["allowed_senders"])
            )
        checked_credentials = (
            validate_credentials(credentials) if replace_credentials else None
        )
        if replace_credentials and checked_credentials is None:
            raise ValueError("credentials are required when replacing them")
        if credentials is not None and not replace_credentials:
            raise ValueError("replacing credentials must be explicit")
        now = utc_now()
        digest = _digest(
            {
                "operation": "update",
                "connection_id": connection_id,
                "expected_revision": expected_revision,
                "changes": clean,
                "credentials": (
                    None
                    if checked_credentials is None
                    else _digest(checked_credentials)
                ),
            }
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._request(connection, request_id, "update", digest)
            if replay is not None:
                replay_result = replay.result or {}
                record = ChannelConnection.model_validate(replay_result["connection"])
                return UpdateResult(
                    connection=record,
                    replayed=True,
                    credentials_replaced=bool(
                        replay_result.get("credentials_replaced", False)
                    ),
                )
            row = connection.execute(
                "SELECT * FROM channel_connections WHERE connection_id = ?",
                (connection_id,),
            ).fetchone()
            if row is None:
                raise ChannelNotFound(connection_id)
            current = self._connection(row)
            if current.revision != expected_revision:
                raise ChannelRevisionConflict(current.revision)
            self._start_request(connection, request_id, "update", digest, now)
            values = current.model_dump(mode="json")
            values.update(clean)
            if values["enabled"] is False:
                values["desired_state"] = ChannelDesiredState.STOPPED.value
            new_ref = current.credential_ref
            if replace_credentials and checked_credentials is not None:
                new_ref = "cred-" + secrets.token_hex(16)
                connection.execute(
                    "INSERT INTO channel_credentials "
                    "(credential_ref, secret_json, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (new_ref, _json(checked_credentials), now, now),
                )
            values.update(
                {
                    "credential_ref": new_ref,
                    "revision": current.revision + 1,
                    "updated_at": now,
                }
            )
            record = ChannelConnection.model_validate(values)
            connection.execute(
                "UPDATE channel_connections SET platform = ?, display_name = ?, "
                "enabled = ?, desired_state = ?, credential_ref = ?, "
                "owner_identity = ?, allowed_senders_json = ?, metadata_json = ?, "
                "revision = ?, updated_at = ? WHERE connection_id = ?",
                (
                    record.platform.value,
                    record.display_name,
                    int(record.enabled),
                    record.desired_state.value,
                    record.credential_ref,
                    record.owner_identity,
                    _json(list(record.allowed_senders)),
                    _json(record.metadata),
                    record.revision,
                    record.updated_at,
                    connection_id,
                ),
            )
            if new_ref != current.credential_ref and current.credential_ref:
                connection.execute(
                    "DELETE FROM channel_credentials WHERE credential_ref = ?",
                    (current.credential_ref,),
                )
            result = record.model_dump(mode="json")
            self._finish_request(
                connection,
                request_id,
                {
                    "connection": result,
                    "credentials_replaced": replace_credentials,
                },
                now,
            )
        return UpdateResult(
            connection=record,
            replayed=False,
            credentials_replaced=replace_credentials,
        )

    def delete_connection(
        self,
        connection_id: str,
        *,
        expected_revision: int,
        request_id: str,
        confirmed: bool,
    ) -> DeleteResult:
        if not confirmed:
            raise ValueError("deleting a channel connection requires confirmation")
        now = utc_now()
        digest = _digest(
            {
                "operation": "delete",
                "connection_id": connection_id,
                "expected_revision": expected_revision,
            }
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._request(connection, request_id, "delete", digest)
            if replay is not None:
                return DeleteResult(connection_id=connection_id, replayed=True)
            row = connection.execute(
                "SELECT * FROM channel_connections WHERE connection_id = ?",
                (connection_id,),
            ).fetchone()
            if row is None:
                raise ChannelNotFound(connection_id)
            current = self._connection(row)
            if current.revision != expected_revision:
                raise ChannelRevisionConflict(current.revision)
            self._start_request(connection, request_id, "delete", digest, now)
            connection.execute(
                "DELETE FROM channel_connections WHERE connection_id = ?",
                (connection_id,),
            )
            if current.credential_ref:
                connection.execute(
                    "DELETE FROM channel_credentials WHERE credential_ref = ?",
                    (current.credential_ref,),
                )
            self._finish_request(
                connection,
                request_id,
                {
                    "deleted": True,
                    "connection_id": connection_id,
                    "audit_retained": True,
                },
                now,
            )
        return DeleteResult(connection_id=connection_id, replayed=False)

    def set_desired_state(
        self, connection_id: str, desired_state: ChannelDesiredState
    ) -> ChannelConnection:
        now = utc_now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM channel_connections WHERE connection_id = ?",
                (connection_id,),
            ).fetchone()
            if row is None:
                raise ChannelNotFound(connection_id)
            updated = connection.execute(
                "UPDATE channel_connections SET desired_state = ?, updated_at = ? "
                "WHERE connection_id = ?",
                (desired_state.value, now, connection_id),
            )
            if updated.rowcount != 1:
                raise ChannelNotFound(connection_id)
            current = self._connection(row).model_copy(
                update={"desired_state": desired_state, "updated_at": now}
            )
        return current

    def claim_request(
        self, request_id: str, *, operation: str, digest_value: object
    ) -> RequestClaim:
        digest = _digest(digest_value)
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._request(connection, request_id, operation, digest)
            if existing is not None:
                return existing
            self._start_request(connection, request_id, operation, digest, now)
        return RequestClaim(replayed=False, result=None)

    def complete_request(self, request_id: str, result: dict[str, Any]) -> None:
        now = utc_now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state FROM channel_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is None or str(row["state"]) != "pending":
                raise ChannelOperationUncertain(
                    "channel request receipt is no longer pending"
                )
            self._finish_request(connection, request_id, result, now)

    def claim_inbound(self, event: ChannelInboundEvent) -> InboundClaim:
        digest = inbound_digest(event)
        now = utc_now()
        with self._connect() as connection:
            inserted = connection.execute(
                "INSERT OR IGNORE INTO channel_inbound "
                "(platform, connection_id, external_message_id, event_digest, "
                "state, received_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'claimed', ?, ?)",
                (
                    event.platform.value,
                    event.connection_id,
                    event.external_message_id,
                    digest,
                    event.received_at,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM channel_inbound WHERE connection_id = ? "
                "AND external_message_id = ?",
                (event.connection_id, event.external_message_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("channel inbound claim disappeared")
            return InboundClaim(
                created=inserted.rowcount == 1,
                state=str(row["state"]),
                event_digest=str(row["event_digest"]),
                conversation_id=(
                    str(row["conversation_id"])
                    if row["conversation_id"] is not None
                    else None
                ),
                run_id=str(row["run_id"]) if row["run_id"] is not None else None,
                error=str(row["error"]) if row["error"] is not None else None,
            )

    def complete_inbound(
        self,
        connection_id: str,
        external_message_id: str,
        *,
        state: str,
        conversation_id: str | None = None,
        run_id: str | None = None,
        error: str | None = None,
    ) -> None:
        with self._connect() as connection:
            updated = connection.execute(
                "UPDATE channel_inbound SET state = ?, conversation_id = ?, "
                "run_id = ?, error = ?, updated_at = ? WHERE connection_id = ? "
                "AND external_message_id = ?",
                (
                    state,
                    conversation_id,
                    run_id,
                    error,
                    utc_now(),
                    connection_id,
                    external_message_id,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError("channel inbound claim disappeared")

    def begin_delivery(
        self,
        *,
        connection_id: str,
        platform: ChannelPlatform,
        target: str,
        message: str,
        idempotency_key: str,
    ) -> tuple[DeliveryRecord, bool]:
        if not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        raw = f"channel-delivery:{connection_id}:{idempotency_key}"
        delivery_id = "del-" + hashlib.sha256(raw.encode()).hexdigest()[:32]
        payload_digest = _payload_digest(target, message)
        now = utc_now()
        with self._connect() as connection:
            inserted = connection.execute(
                "INSERT OR IGNORE INTO channel_deliveries "
                "(delivery_id, connection_id, platform, idempotency_key, "
                "target_digest, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'sending', ?, ?)",
                (
                    delivery_id,
                    connection_id,
                    platform.value,
                    idempotency_key,
                    payload_digest,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM channel_deliveries WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("channel delivery claim disappeared")
            record = self._delivery(row)
            if inserted.rowcount != 1 and record.target_digest != payload_digest:
                raise ChannelRequestConflict(
                    "idempotency key was reused for a different message"
                )
            return record, inserted.rowcount == 1

    def complete_delivery(
        self, delivery_id: str, status: ChannelSendStatus, error: str | None = None
    ) -> DeliveryRecord:
        with self._connect() as connection:
            updated = connection.execute(
                "UPDATE channel_deliveries SET status = ?, error = ?, updated_at = ? "
                "WHERE delivery_id = ?",
                (status.value, error, utc_now(), delivery_id),
            )
            if updated.rowcount != 1:
                raise ChannelNotFound(delivery_id)
            row = connection.execute(
                "SELECT * FROM channel_deliveries WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
        if row is None:
            raise ChannelNotFound(delivery_id)
        return self._delivery(row)

    def mark_inflight_deliveries_unknown(self) -> int:
        with self._connect() as connection:
            updated = connection.execute(
                "UPDATE channel_deliveries SET status = 'unknown', "
                "error = 'process_interrupted', updated_at = ? "
                "WHERE status = 'sending'",
                (utc_now(),),
            )
            return int(updated.rowcount)

    def list_deliveries(
        self, connection_id: str | None = None
    ) -> tuple[DeliveryRecord, ...]:
        with self._connect() as connection:
            if connection_id is None:
                rows = connection.execute(
                    "SELECT * FROM channel_deliveries ORDER BY created_at, delivery_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM channel_deliveries WHERE connection_id = ? "
                    "ORDER BY created_at, delivery_id",
                    (connection_id,),
                ).fetchall()
        return tuple(self._delivery(row) for row in rows)

    def load_credentials(self, credential_ref: str | None) -> dict[str, str] | None:
        if credential_ref is None:
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT secret_json FROM channel_credentials WHERE credential_ref = ?",
                (credential_ref,),
            ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(str(row["secret_json"]))
        except ValueError:
            return None
        if not isinstance(value, dict):
            return None
        return {
            str(key): str(item)
            for key, item in value.items()
            if isinstance(key, str) and isinstance(item, str)
        }


__all__ = [
    "ChannelStore",
    "CreateResult",
    "DeleteResult",
    "DeliveryRecord",
    "InboundClaim",
    "RequestClaim",
    "UpdateResult",
]
