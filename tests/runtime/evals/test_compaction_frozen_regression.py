"""The offline scripted compaction regression stays frozen and deterministic.

Adding an opt-in real-model comparison must not alter the offline mechanism
regression: its cases, runner and tests are pinned by content and continue to run
provider-free. This guards against a live-comparison change silently rewriting the
deterministic oracle.
"""

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

FROZEN = {
    "evaluation/cases/context_compaction.py": (
        "2e0de6540894cbd9f5c4fb6a661ccc7884b5d7c7"
    ),
    "evaluation/runners/run_context_compaction.py": (
        "5341fe9f8b0f1b27cfdf960c73d5e2564289324f"
    ),
    "tests/runtime/evals/test_context_compaction_cases.py": (
        "be75dd3aa8eee5c5ad0ebb245751dcf623b1b794"
    ),
}


def _git_blob_hash(path: Path) -> str:
    data = path.read_bytes()
    header = f"blob {len(data)}\0".encode()
    return hashlib.sha1(header + data).hexdigest()


def test_offline_compaction_assets_are_unchanged() -> None:
    for relative, expected in FROZEN.items():
        path = ROOT / relative
        assert path.is_file(), f"missing frozen asset: {relative}"
        assert _git_blob_hash(path) == expected, (
            f"{relative} changed; the offline compaction regression is frozen. "
            "A new comparison must not rewrite it."
        )
