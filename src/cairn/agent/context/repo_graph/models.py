"""Immutable facts and explicit resource limits for repository inspection."""

from dataclasses import dataclass
from enum import StrEnum


class Language(StrEnum):
    PYTHON = "python"
    JAVASCRIPT = "javascript"
    TYPESCRIPT = "typescript"
    JAVA = "java"
    KOTLIN = "kotlin"
    GO = "go"
    RUST = "rust"
    TOML = "toml"
    JSON = "json"
    XML = "xml"
    TEXT = "text"
    UNKNOWN = "unknown"


class FileKind(StrEnum):
    SOURCE = "source"
    MANIFEST = "manifest"
    TEXT = "text"
    BINARY = "binary"
    UNKNOWN = "unknown"


class FileStatus(StrEnum):
    READABLE = "readable"
    BINARY = "binary"
    OVERSIZED = "oversized"
    UNREADABLE = "unreadable"
    SYMLINK = "symlink"
    SPECIAL = "special"
    CHANGED = "changed"
    IO_LIMIT = "io_limit"


class ManifestKind(StrEnum):
    PYTHON = "python"
    NODE = "node"
    JAVA = "java"
    GRADLE = "gradle"
    GO = "go"
    RUST = "rust"


class ManifestStatus(StrEnum):
    PARSED = "parsed"
    MALFORMED = "malformed"
    PRESENCE_ONLY = "presence_only"
    LIMITED = "limited"
    UNSUPPORTED = "unsupported"


class IssueReason(StrEnum):
    ENTRY_LIMIT = "entry_limit"
    DEPTH_LIMIT = "depth_limit"
    IO_LIMIT = "io_limit"
    OVERSIZED = "oversized"
    UNREADABLE = "unreadable"
    CHANGED = "changed"
    MALFORMED_MANIFEST = "malformed_manifest"
    MANIFEST_LIMIT = "manifest_limit"
    UNSUPPORTED_MANIFEST = "unsupported_manifest"
    INVALID_PYTHON = "invalid_python"
    STRUCTURE_LIMIT = "structure_limit"


class SkipReason(StrEnum):
    EXCLUDED = "excluded"
    SYMLINK = "symlink"
    SPECIAL = "special"
    BINARY = "binary"


class PythonStatus(StrEnum):
    PARSED = "parsed"
    INVALID = "invalid"
    LIMITED = "limited"
    UNSUPPORTED = "unsupported"


class SymbolKind(StrEnum):
    CLASS = "class"
    FUNCTION = "function"
    ASYNC_FUNCTION = "async_function"


class RelationshipStatus(StrEnum):
    RESOLVED = "resolved"
    EXTERNAL = "external"
    UNRESOLVED = "unresolved"
    AMBIGUOUS = "ambiguous"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class BuildLimits:
    max_entries: int = 10_000
    max_depth: int = 32
    max_file_bytes: int = 512 * 1024
    max_total_bytes: int = 16 * 1024 * 1024
    max_manifest_items: int = 128
    max_issues: int = 100
    max_ast_nodes: int = 50_000
    max_structure_records: int = 10_000

    def __post_init__(self) -> None:
        for field in self.__dataclass_fields__:
            value = getattr(self, field)
            if type(value) is not int or value < 1:
                raise ValueError(f"{field} must be a positive integer")


@dataclass(frozen=True, slots=True)
class FileRecord:
    path: str
    language: Language
    kind: FileKind
    status: FileStatus
    size_bytes: int
    source_sha256: str | None


@dataclass(frozen=True, slots=True)
class DirectoryRecord:
    path: str


@dataclass(frozen=True, slots=True)
class LanguageRecord:
    language: Language
    file_count: int


@dataclass(frozen=True, slots=True)
class ManifestRecord:
    path: str
    kind: ManifestKind
    status: ManifestStatus
    source_sha256: str
    name: str | None
    dependencies: tuple[str, ...]
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class BuildIssue:
    reason: IssueReason
    path: str


@dataclass(frozen=True, slots=True)
class SkipCount:
    reason: SkipReason
    count: int


@dataclass(frozen=True, slots=True)
class BuildReport:
    complete: bool
    entries_seen: int
    bytes_read: int
    skipped: tuple[SkipCount, ...]
    issues: tuple[BuildIssue, ...]
    omitted_issues: int = 0


@dataclass(frozen=True, slots=True)
class ModuleRecord:
    path: str
    name: str | None
    is_package: bool
    status: PythonStatus
    source_sha256: str


@dataclass(frozen=True, slots=True)
class SymbolRecord:
    path: str
    name: str
    kind: SymbolKind
    line: int
    end_line: int
    source_sha256: str


@dataclass(frozen=True, slots=True)
class ImportRecord:
    path: str
    module: str | None
    names: tuple[str, ...]
    level: int
    line: int
    end_line: int
    source_sha256: str


@dataclass(frozen=True, slots=True)
class DependencyRecord:
    path: str
    module: str | None
    target_path: str | None
    status: RelationshipStatus
    line: int
    end_line: int
    source_sha256: str
