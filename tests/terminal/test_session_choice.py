"""Choosing between Sessions when an abbreviated id matches more than one.

An ambiguous prefix used to be a flat refusal. That is right for a library call,
but wrong for an operator who is looking at the listing and can tell the
conversations apart, so the CLI turns the refusal into a choice. These tests pin
the choice half: which candidates are offered, and what declining means.
"""

import uuid
from pathlib import Path

import pytest

from cairn.infra.sessions.models import SessionStoreError
from cairn.terminal.durable import _select_session
from tests.support.sessions import binding, store


def _populate(root: Path, ids: list[str]) -> None:
    writer = store(root)
    try:
        for session_id in ids:
            writer.create(binding(root, session_id=session_id))
    finally:
        writer.close()


def _shared_prefix_ids() -> list[str]:
    return [
        "abcdef01-0000-4000-8000-000000000001",
        "abcdef01-1111-4000-8000-000000000002",
    ]


def test_matching_sessions_lists_every_candidate(tmp_path: Path) -> None:
    ids = _shared_prefix_ids()
    _populate(tmp_path, ids)
    reader = store(tmp_path, mode="inspect")
    try:
        matches = reader.matching_sessions("abcdef01")
    finally:
        reader.close()

    assert {summary.session_id for summary in matches} == set(ids)


def test_matching_sessions_rejects_non_hex(tmp_path: Path) -> None:
    _populate(tmp_path, _shared_prefix_ids())
    reader = store(tmp_path, mode="inspect")
    try:
        with pytest.raises(SessionStoreError, match="invalid_session_id"):
            reader.matching_sessions("not-hex")
    finally:
        reader.close()


def test_a_unique_prefix_is_not_offered_as_a_choice(tmp_path: Path) -> None:
    """One candidate resolves without asking, so a picker never appears."""
    ids = [str(uuid.uuid4())]
    _populate(tmp_path, ids)
    reader = store(tmp_path, mode="inspect")
    asked: list[int] = []

    async def choose(candidates: object) -> str | None:
        asked.append(1)
        return None

    try:
        resolved = _run(_select_session(reader, ids[0][:6], choose))
    finally:
        reader.close()

    assert resolved == ids[0]
    assert asked == []


def test_an_ambiguous_prefix_offers_the_candidates(tmp_path: Path) -> None:
    ids = _shared_prefix_ids()
    _populate(tmp_path, ids)
    reader = store(tmp_path, mode="inspect")
    offered: list[tuple[str, ...]] = []

    async def choose(candidates: tuple[object, ...]) -> str | None:
        offered.append(tuple(c.session_id for c in candidates))  # type: ignore[attr-defined]
        return ids[1]

    try:
        resolved = _run(_select_session(reader, "abcdef01", choose))
    finally:
        reader.close()

    assert resolved == ids[1]
    assert set(offered[0]) == set(ids)


def test_declining_the_choice_selects_nothing(tmp_path: Path) -> None:
    _populate(tmp_path, _shared_prefix_ids())
    reader = store(tmp_path, mode="inspect")

    async def choose(candidates: object) -> str | None:
        return None

    try:
        with pytest.raises(SessionStoreError, match="session_selection_cancelled"):
            _run(_select_session(reader, "abcdef01", choose))
    finally:
        reader.close()


def test_without_a_picker_an_ambiguous_prefix_still_refuses(tmp_path: Path) -> None:
    """A non-interactive caller keeps the fail-closed refusal."""
    _populate(tmp_path, _shared_prefix_ids())
    reader = store(tmp_path, mode="inspect")
    try:
        with pytest.raises(SessionStoreError, match="ambiguous_session_id"):
            _run(_select_session(reader, "abcdef01", None))
    finally:
        reader.close()


def test_an_unknown_prefix_still_reports_not_found(tmp_path: Path) -> None:
    _populate(tmp_path, _shared_prefix_ids())
    reader = store(tmp_path, mode="inspect")
    try:
        with pytest.raises(SessionStoreError, match="session_not_found"):
            _run(_select_session(reader, "ffffee", None))
    finally:
        reader.close()


def _run(coroutine: object) -> str:
    import asyncio

    return asyncio.run(coroutine)  # type: ignore[arg-type]
