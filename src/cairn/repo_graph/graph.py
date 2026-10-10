"""A snapshot describes one bounded inspection, never implicit live freshness."""

from dataclasses import dataclass
from pathlib import Path

from cairn.repo_graph.models import (
    BuildReport,
    DirectoryRecord,
    FileRecord,
    LanguageRecord,
    ManifestRecord,
)


@dataclass(frozen=True, slots=True)
class RepoGraph:
    root: Path
    files: tuple[FileRecord, ...]
    directories: tuple[DirectoryRecord, ...]
    languages: tuple[LanguageRecord, ...]
    manifests: tuple[ManifestRecord, ...]
    report: BuildReport
