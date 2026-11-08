"""Host-private WeChat account/state, outside every Agent-writable workspace.

The bot token, poll cursor and per-message facts live here. Files are 0600 in a
0700 directory; the default is ``$XDG_STATE_HOME/cairn/wechat`` (usually
``~/.local/state/cairn/wechat``). Nothing here is ever put in the repository, a
Workspace, a trace or a model request.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import socket
import sys
import tempfile
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STATE_DIR_ENV = "CAIRN_WECHAT_STATE_DIR"
STATE_VERSION = 1

ADMISSION_STATUSES = frozenset(
    {"awaiting_admission", "accepted", "rejected", "admission_unknown"}
)
DELIVERY_STATUSES = frozenset({"pending", "started", "sent", "unconfirmed"})


def default_state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "cairn" / "wechat"


def ensure_outside_workspace(state_root: Path, workspace: Path) -> None:
    """Refuse a host state directory the Agent could reach through the Workspace."""
    resolved_state = state_root.resolve()
    resolved_workspace = workspace.resolve()
    if resolved_state == resolved_workspace or resolved_state.is_relative_to(
        resolved_workspace
    ):
        raise ValueError(
            "WeChat state directory must live outside the Agent-writable Workspace"
        )


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):
        path.parent.chmod(0o700)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp_name)
        raise


@dataclass(frozen=True, slots=True)
class WechatAccount:
    bot_token: str
    ilink_bot_id: str
    base_url: str
    ilink_user_id: str = ""


class WechatState:
    """Single-account durable facts for login, polling and task admission."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.account_path = root / "account.json"
        self.state_path = root / "state.json"
        self._state: dict[str, Any] | None = None
        self._delivery_lock = threading.RLock()

    # -- account ---------------------------------------------------------
    def load_account(self) -> WechatAccount | None:
        try:
            raw = json.loads(self.account_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError):
            raise ValueError("WeChat account state is unreadable") from None
        if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
            raise ValueError("WeChat account state has an unsupported shape/version")
        try:
            return WechatAccount(
                bot_token=str(raw["bot_token"]),
                ilink_bot_id=str(raw["ilink_bot_id"]),
                base_url=str(raw["base_url"]),
                ilink_user_id=str(raw.get("ilink_user_id", "")),
            )
        except KeyError:
            raise ValueError("WeChat account state is incomplete") from None

    def clear_connection(self) -> None:
        """Remove the local account/cursor so the next /wx performs a new login."""
        for path in (self.account_path, self.state_path):
            path.unlink(missing_ok=True)
        self._state = None

    def save_account(self, account: WechatAccount) -> None:
        _atomic_write(
            self.account_path,
            {
                "version": STATE_VERSION,
                "bot_token": account.bot_token,
                "ilink_bot_id": account.ilink_bot_id,
                "base_url": account.base_url,
                "ilink_user_id": account.ilink_user_id,
            },
        )

    # -- poll/task state -------------------------------------------------
    @staticmethod
    def _normalise_delivery_intent(key: str, record: object) -> dict[str, Any]:
        if not isinstance(record, dict):
            raise ValueError("WeChat delivery intent is damaged")
        if "admission_status" in record or "delivery_status" in record:
            admission = record.get("admission_status")
            delivery = record.get("delivery_status")
            if admission not in ADMISSION_STATUSES or delivery not in DELIVERY_STATUSES:
                raise ValueError("WeChat delivery intent has an unknown state")
            if record.get("intent_key") != key:
                raise ValueError("WeChat delivery intent key is damaged")
            required = ("message_key", "request_id", "account_digest", "sender_digest")
        else:
            # Convert the short-lived B2-B1 shape explicitly.  This is not a
            # silent fallback for malformed data.
            legacy = record.get("status")
            if legacy not in DELIVERY_STATUSES:
                raise ValueError("WeChat delivery intent has an unknown legacy state")
            record = {
                **record,
                "intent_key": key,
                "message_key": record.get("message_key", ""),
                "request_id": record.get("request_id", ""),
                "admission_status": (
                    "awaiting_admission" if legacy == "pending" else "accepted"
                ),
                "delivery_status": legacy,
            }
            required = ("message_key", "request_id", "account_digest", "sender_digest")
        if any(
            not isinstance(record.get(field), str) or not record[field]
            for field in required
        ):
            raise ValueError("WeChat delivery intent is damaged")
        for field in ("message_digest", "content_digest"):
            if not isinstance(record.get(field), str) or not record[field]:
                raise ValueError("WeChat delivery intent is damaged")
        if not isinstance(record.get("context_token"), str):
            raise ValueError("WeChat delivery intent is damaged")
        created = record.get("created_at")
        updated = record.get("updated_at")
        if not isinstance(created, (int, float)) or not isinstance(
            updated, (int, float)
        ):
            raise ValueError("WeChat delivery intent is damaged")
        return record

    def load(self) -> dict[str, Any]:
        if self._state is not None:
            return self._state
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            # First run: initialise a valid empty state. Only an *existing*
            # damaged file is a fail-closed error.
            self._state = {
                "version": STATE_VERSION,
                "cursor": "",
                "next_sequence": 1,
                "messages": {},
                "delivery_intents": {},
            }
            return self._state
        except (OSError, json.JSONDecodeError):
            raise ValueError("WeChat runtime state is unreadable") from None
        if not isinstance(raw, dict):
            raise ValueError("WeChat runtime state is malformed")
        if raw.get("version") != STATE_VERSION:
            raise ValueError("WeChat runtime state has an unsupported version")
        cursor = raw.get("cursor")
        messages = raw.get("messages")
        delivery_intents = raw.get("delivery_intents", {})
        if not isinstance(cursor, str) or not isinstance(messages, dict):
            # A damaged state file must fail closed: silently starting from an
            # empty dedupe map could re-execute an already-started task.
            raise ValueError("WeChat runtime state is damaged")
        if not isinstance(delivery_intents, dict):
            raise ValueError("WeChat delivery state is damaged")
        delivery_intents = {
            key: self._normalise_delivery_intent(key, record)
            for key, record in delivery_intents.items()
        }
        sequences: set[int] = set()
        for _key, record in messages.items():
            if not isinstance(record, dict):
                raise ValueError("WeChat message record is damaged")
            if not isinstance(record.get("state"), str):
                raise ValueError("WeChat message record is damaged")
            if "sequence" not in record:
                continue
            sequence = record["sequence"]
            if type(sequence) is not int or sequence < 1 or sequence in sequences:
                raise ValueError("WeChat message ordering is damaged")
            sequences.add(sequence)
        next_sequence = raw.get("next_sequence", max(sequences, default=0) + 1)
        if type(next_sequence) is not int or next_sequence <= max(sequences, default=0):
            raise ValueError("WeChat message ordering is damaged")
        self._state = {
            "version": STATE_VERSION,
            "cursor": cursor,
            "next_sequence": next_sequence,
            "messages": messages,
            "delivery_intents": delivery_intents,
        }
        return self._state

    def flush(self) -> None:
        state = self.load()
        _atomic_write(self.state_path, state)

    def get_cursor(self) -> str:
        return str(self.load().get("cursor", ""))

    def set_cursor(self, cursor: str) -> None:
        self.load()["cursor"] = cursor
        self.flush()

    def latest_context(self, sender: str) -> str | None:
        """Latest reply context for a sender, for owner-directed delivery."""
        latest: tuple[float, str] | None = None
        for record in self.load().get("messages", {}).values():
            if not isinstance(record, dict) or record.get("sender") != sender:
                continue
            context = record.get("context_token")
            updated = record.get("updated")
            if isinstance(context, str) and context:
                stamp = float(updated) if isinstance(updated, (int, float)) else 0.0
                if latest is None or stamp >= latest[0]:
                    latest = (stamp, context)
        return latest[1] if latest is not None else None

    def get_record(self, key: str) -> dict[str, Any] | None:
        record = self.load()["messages"].get(key)
        return record if isinstance(record, dict) else None

    def begin_delivery_intent(
        self,
        *,
        message_key: str,
        message_id: str,
        request_id: str,
        account_id: str,
        sender_id: str,
        context_token: str,
        content: str,
    ) -> str:
        """Persist the inbound message before Runtime admission is attempted."""
        if not message_id or not request_id or not account_id or not sender_id:
            raise ValueError(
                "Delivery requires stable message, request, account and sender identity"
            )
        message_digest = hashlib.sha256(message_id.encode()).hexdigest()
        account_digest = hashlib.sha256(account_id.encode()).hexdigest()
        sender_digest = hashlib.sha256(sender_id.encode()).hexdigest()
        content_digest = hashlib.sha256(content.encode()).hexdigest()
        key = hashlib.sha256(message_key.encode()).hexdigest()
        with self._delivery_lock:
            intents = self.load().setdefault("delivery_intents", {})
            for other_key, current in intents.items():
                if not isinstance(current, dict):
                    raise ValueError("WeChat delivery intent is damaged")
                same_message = current.get("message_digest") == message_digest
                same_request = current.get("request_id") == request_id
                if not same_message and not same_request:
                    continue
                if (
                    not same_message
                    or current.get("account_digest") != account_digest
                    or current.get("sender_digest") != sender_digest
                    or current.get("content_digest") != content_digest
                    or current.get("request_id") != request_id
                ):
                    raise ValueError(
                        "WeChat message identity was reused with different content"
                    )
                return str(other_key)
            if key in intents:
                raise ValueError("WeChat delivery intent key is damaged")
            intents[key] = {
                "intent_key": key,
                "message_key": message_key,
                "message_digest": message_digest,
                "request_id": request_id,
                "account_digest": account_digest,
                "sender_digest": sender_digest,
                "content_digest": content_digest,
                "context_token": context_token,
                "conversation_id": None,
                "run_id": None,
                "admission_status": "awaiting_admission",
                "delivery_status": "pending",
                "admission_error": None,
                "delivery_error": None,
                "created_at": time.time(),
                "updated_at": time.time(),
            }
            self.flush()
            return key

    def bind_delivery_intent(
        self,
        key: str,
        *,
        conversation_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        with self._delivery_lock:
            intents = self.load().get("delivery_intents", {})
            current = intents.get(key)
            if not isinstance(current, dict):
                raise ValueError("WeChat delivery intent not found")
            for field, value in (
                ("conversation_id", conversation_id),
                ("run_id", run_id),
            ):
                if value is None:
                    continue
                old = current.get(field)
                if old is not None and old != value:
                    raise ValueError(f"WeChat delivery intent {field} is immutable")
                current[field] = value
            if run_id is not None:
                current["admission_status"] = "accepted"
            current["updated_at"] = time.time()
            self.flush()
            return dict(current)

    def set_admission_status(
        self, key: str, status: str, *, error: str | None = None
    ) -> dict[str, Any]:
        if status not in ADMISSION_STATUSES:
            raise ValueError("Unknown WeChat admission status")
        with self._delivery_lock:
            intents = self.load().get("delivery_intents", {})
            current = intents.get(key)
            if not isinstance(current, dict):
                raise ValueError("WeChat delivery intent not found")
            current_status = current.get("admission_status")
            allowed = {
                "awaiting_admission": {"accepted", "rejected", "admission_unknown"},
                "admission_unknown": {"accepted", "rejected"},
                "accepted": {"accepted"},
                "rejected": {"rejected"},
            }
            if current_status not in allowed or status not in allowed[current_status]:
                raise ValueError(
                    f"Cannot move WeChat admission from {current_status} to {status}"
                )
            current["admission_status"] = status
            current["admission_error"] = error
            current["updated_at"] = time.time()
            self.flush()
            return dict(current)

    def begin_delivery_attempt(self, key: str) -> bool:
        with self._delivery_lock:
            intents = self.load().get("delivery_intents", {})
            current = intents.get(key)
            if not isinstance(current, dict):
                raise ValueError("WeChat delivery intent not found")
            if current.get("admission_status") != "accepted":
                return False
            if current.get("delivery_status") != "pending":
                return False
            current["delivery_status"] = "started"
            current["updated_at"] = time.time()
            self.flush()
            return True

    def mark_delivery_sent(self, key: str) -> dict[str, Any]:
        with self._delivery_lock:
            intents = self.load().get("delivery_intents", {})
            current = intents.get(key)
            if not isinstance(current, dict):
                raise ValueError("WeChat delivery intent not found")
            if current.get("delivery_status") == "sent":
                return dict(current)
            if current.get("delivery_status") != "started":
                raise ValueError("WeChat delivery must be started before sent")
            current["delivery_status"] = "sent"
            current["delivery_error"] = None
            current["updated_at"] = time.time()
            self.flush()
            return dict(current)

    def mark_delivery_unconfirmed(self, key: str) -> dict[str, Any]:
        with self._delivery_lock:
            intents = self.load().get("delivery_intents", {})
            current = intents.get(key)
            if not isinstance(current, dict):
                raise ValueError("WeChat delivery intent not found")
            if current.get("delivery_status") == "sent":
                raise ValueError("A sent delivery cannot become unconfirmed")
            current["delivery_status"] = "unconfirmed"
            current["updated_at"] = time.time()
            self.flush()
            return dict(current)

    def get_delivery_intent(self, key: str) -> dict[str, Any] | None:
        intent = self.load().get("delivery_intents", {}).get(key)
        return dict(intent) if isinstance(intent, dict) else None

    def list_delivery_intents(
        self, *, statuses: frozenset[str] | None = None
    ) -> list[dict[str, Any]]:
        intents = self.load().get("delivery_intents", {})
        rows = [dict(intent) for intent in intents.values() if isinstance(intent, dict)]
        if statuses is not None:
            rows = [row for row in rows if row.get("delivery_status") in statuses]
        return sorted(rows, key=lambda row: float(row.get("created_at", 0)))

    def record_received(
        self,
        key: str,
        *,
        sender: str,
        context_token: str,
        text: str,
        stable: bool = True,
        is_text: bool = True,
    ) -> bool:
        """Persist a new received fact; False when the key already exists."""
        state = self.load()
        messages = state["messages"]
        if key in messages:
            return False
        sequence = state["next_sequence"]
        state["next_sequence"] = sequence + 1
        messages[key] = {
            "state": "received",
            "sequence": sequence,
            "sender": sender,
            "context_token": context_token,
            "text": text,
            "stable": stable,
            "is_text": is_text,
            "updated": time.time(),
            "received_at": time.time(),
        }
        self.flush()
        return True

    def mark(self, key: str, state: str, **fields: Any) -> None:
        messages = self.load()["messages"]
        record = messages.get(key, {})
        if not isinstance(record, dict):
            record = {}
        record["state"] = state
        record["updated"] = time.time()
        record.update(fields)
        messages[key] = record
        self.flush()

    def resolve_clarification(
        self,
        original_key: str,
        reply_key: str,
        *,
        target_state: str,
        context_token: str,
    ) -> bool:
        """Consume a clarification reply and transition its original once."""
        messages = self.load()["messages"]
        original = messages.get(original_key)
        reply = messages.get(reply_key)
        if (
            not isinstance(original, dict)
            or not isinstance(reply, dict)
            or original.get("state") != "clarifying"
            or reply.get("state") != "received"
        ):
            return False
        now = time.time()
        original.update(state=target_state, context_token=context_token, updated=now)
        reply.update(
            state="clarification_consumed", context_token=context_token, updated=now
        )
        self.flush()
        return True

    def records_in_state(self, state: str) -> list[tuple[str, dict[str, Any]]]:
        return self.records_in_states(frozenset({state}))

    def records_in_states(
        self, states: frozenset[str]
    ) -> list[tuple[str, dict[str, Any]]]:
        """Observe stable membership while the owner admits new messages.

        Records remain owner-managed live values; this is not a worker write API.
        """
        messages = self.load()["messages"].copy()
        records = [
            (key, record)
            for key, record in messages.items()
            if isinstance(record, dict) and record.get("state") in states
        ]

        def order(item: tuple[str, dict[str, Any]]) -> tuple[int, float | int, str]:
            key, record = item
            sequence = record.get("sequence")
            if type(sequence) is int:
                return (1, sequence, key)
            updated = record.get("updated")
            return (
                0,
                float(updated) if isinstance(updated, (int, float)) else 0.0,
                key,
            )

        return sorted(records, key=order)


class WechatLockError(RuntimeError):
    """Another serve process already owns this account state."""


class WechatServeLock:
    """Host-side exclusive lock held for one serve process lifetime."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.path = state_dir / "serve.lock"
        self._fd: int | None = None
        self._account_socket: socket.socket | None = None

    def acquire(self, *, description: str, account_id: str | None = None) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with suppress(OSError):
            self.state_dir.chmod(0o700)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise WechatLockError(
                "another WeChat serve process already owns this account/workspace"
            ) from None
        try:
            if account_id is not None:
                if not sys.platform.startswith("linux"):
                    raise WechatLockError(
                        "Host-wide WeChat account ownership requires Linux"
                    )
                owner = socket.socket(socket.AF_UNIX)
                self._account_socket = owner
                ident = hashlib.sha256(account_id.encode()).hexdigest()
                try:
                    owner.bind("\0cairn-wechat-" + ident)
                except OSError:
                    raise WechatLockError(
                        "Another process already owns this WeChat account"
                    ) from None
            os.ftruncate(fd, 0)
            os.write(fd, (description + "\n").encode("utf-8"))
            os.fsync(fd)
            self._fd = fd
        except BaseException:
            os.close(fd)
            if self._account_socket is not None:
                self._account_socket.close()
                self._account_socket = None
            raise

    def release(self) -> None:
        if self._account_socket is not None:
            self._account_socket.close()
            self._account_socket = None
        if self._fd is None:
            return
        with suppress(OSError):
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        with suppress(OSError):
            os.close(self._fd)
        self._fd = None
