"""The one SQLite WAL-reset policy shared by every WAL-mode store.

Durable and Project Memory both open databases in WAL mode, so both inherit the
same runtime requirement: SQLite must carry the reviewed WAL-reset fix. This
module holds that single version judgement and nothing else -- no connection, no
schema, no lifecycle, no driver. Each store keeps its own connection, transaction,
errors and lifecycle and calls in here only to ask a version question.

Keep the answer in one place. A second copy of this comparison is how one store
silently drifts out from under the other's release rule.
"""

import os
import sqlite3
from typing import Literal

#: The one declaration that admits an unverified SQLite runtime for development.
#: It admits the session store; see ``wal_admission`` for what else may consult it.
UNVERIFIED_SQLITE_DECLARATION = "CAIRN_ALLOW_UNVERIFIED_SQLITE"

#: Versions carrying the reviewed WAL-reset fix, as an operator-facing string.
#: Kept beside ``sqlite_wal_fix_supported``; one test pins both together.
VERIFIED_SQLITE_VERSIONS = "3.51.3+, 3.50.7+ or 3.44.6+"

SqliteAdmission = Literal["verified", "unverified", "refused"]


class AdmissionOptionError(ValueError):
    """A caller passed a non-boolean admission option.

    Neutral on purpose: this module must not import a store-specific error type.
    Stores translate it into their own contract.
    """


def sqlite_wal_fix_supported(version: tuple[int, int, int] | None = None) -> bool:
    """Release gate, not a runtime upgrade or claim that older SQLite is safe."""
    v = sqlite3.sqlite_version_info if version is None else version
    return (
        v >= (3, 51, 3)
        or (v[:2] == (3, 50) and v[2] >= 7)
        or (v[:2] == (3, 44) and v[2] >= 6)
    )


def unverified_declared() -> bool:
    """Whether the host declared an unverified runtime for this process."""
    return os.environ.get(UNVERIFIED_SQLITE_DECLARATION) == "1"


def wal_admission(
    *,
    allow_unverified: bool = False,
    version: tuple[int, int, int] | None = None,
    honor_declaration: bool = True,
) -> SqliteAdmission:
    """Decide runtime admission once: verified, declared, or refused.

    A verified runtime needs no declaration. An unverified one is refused unless
    the host declares it, and then stays ``unverified``: admission is never a
    claim that the runtime is supported.

    ``honor_declaration`` exists because the development declaration was
    introduced for the session store. A store that must not inherit that escape
    hatch passes ``False`` and then only ever sees ``verified`` or ``refused``.
    """
    if type(allow_unverified) is not bool or type(honor_declaration) is not bool:
        raise AdmissionOptionError("admission options must be actual booleans")
    if sqlite_wal_fix_supported(version):
        return "verified"
    if allow_unverified or (honor_declaration and unverified_declared()):
        return "unverified"
    return "refused"
