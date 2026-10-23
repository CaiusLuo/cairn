"""Listing stored Sessions and resolving the abbreviated id a listing shows.

Listing exists so an operator can find a conversation to reopen without knowing
its identifier. Resolution is the other half: a typed prefix must select exactly
one Session, because reopening the wrong conversation would resume work against
the wrong history.
"""

import uuid
from pathlib import Path

import pytest

from cairn.infra.sessions.models import (
    MAX_LISTED_SESSIONS,
    SESSION_SHORT_ID_LENGTH,
    SessionStoreError,
)
from cairn.infra.sessions.store import SessionStore
from tests.support.sessions import binding, store


def _populate(root: Path, count: int) -> list[str]:
    ids = [str(uuid.uuid4()) for _ in range(count)]
    writer = store(root)
    try:
        for session_id in ids:
            writer.create(binding(root, session_id=session_id))
    finally:
        writer.close()
    return ids


def _reader(root: Path) -> SessionStore:
    return store(root, mode="inspect")


def test_listing_reports_stored_sessions(tmp_path: Path) -> None:
    ids = _populate(tmp_path, 2)
    reader = _reader(tmp_path)
    try:
        listed = reader.list_sessions()
    finally:
        reader.close()

    assert {summary.session_id for summary in listed} == set(ids)
    assert all(summary.state == "ready" for summary in listed)
    assert all(summary.title is None for summary in listed)


def test_listing_shows_the_short_id_prefix(tmp_path: Path) -> None:
    ids = _populate(tmp_path, 1)
    reader = _reader(tmp_path)
    try:
        summary = reader.list_sessions()[0]
    finally:
        reader.close()

    assert summary.session_id == ids[0]
    assert summary.short_id == ids[0][:SESSION_SHORT_ID_LENGTH]
    assert len(summary.short_id) == SESSION_SHORT_ID_LENGTH


def test_a_short_id_resolves_to_its_session(tmp_path: Path) -> None:
    ids = _populate(tmp_path, 2)
    reader = _reader(tmp_path)
    try:
        for session_id in ids:
            assert reader.resolve_session_id(session_id[:6]) == session_id
    finally:
        reader.close()


@pytest.mark.parametrize("hyphenated", [True, False])
def test_a_full_id_resolves_in_either_form(tmp_path: Path, hyphenated: bool) -> None:
    ids = _populate(tmp_path, 1)
    typed = ids[0] if hyphenated else ids[0].replace("-", "")
    reader = _reader(tmp_path)
    try:
        assert reader.resolve_session_id(typed) == ids[0]
    finally:
        reader.close()


def test_an_unknown_prefix_is_reported(tmp_path: Path) -> None:
    _populate(tmp_path, 1)
    reader = _reader(tmp_path)
    try:
        with pytest.raises(SessionStoreError, match="session_not_found"):
            reader.resolve_session_id("ffffffff")
    finally:
        reader.close()


def test_a_non_hex_prefix_is_refused_before_any_query(tmp_path: Path) -> None:
    _populate(tmp_path, 1)
    reader = _reader(tmp_path)
    try:
        with pytest.raises(SessionStoreError, match="invalid_session_id"):
            reader.resolve_session_id("not-hex")
    finally:
        reader.close()


def test_an_ambiguous_prefix_names_no_session(tmp_path: Path) -> None:
    """Two Sessions sharing a prefix must not silently pick one."""
    root = tmp_path
    # Same first 8 characters, different tail: the short prefix is ambiguous and
    # a longer one is not.
    shared = "abcdef01"
    ids = [
        f"{shared}-0000-4000-8000-000000000001",
        f"{shared}-1111-4000-8000-000000000002",
    ]
    writer = store(root)
    try:
        for session_id in ids:
            writer.create(binding(root, session_id=session_id))
    finally:
        writer.close()

    reader = _reader(root)
    try:
        with pytest.raises(SessionStoreError, match="ambiguous_session_id"):
            reader.resolve_session_id(shared)
        # A longer prefix that is unique still resolves.
        assert reader.resolve_session_id(ids[0][:10]) == ids[0]
    finally:
        reader.close()


def test_listing_is_bounded(tmp_path: Path) -> None:
    _populate(tmp_path, 3)
    reader = _reader(tmp_path)
    try:
        assert len(reader.list_sessions(limit=2)) == 2
        assert len(reader.list_sessions()) <= MAX_LISTED_SESSIONS
    finally:
        reader.close()


@pytest.mark.parametrize("limit", [0, -1])
def test_a_nonpositive_limit_is_refused(tmp_path: Path, limit: int) -> None:
    _populate(tmp_path, 1)
    reader = _reader(tmp_path)
    try:
        with pytest.raises(SessionStoreError, match="invalid_options"):
            reader.list_sessions(limit=limit)
    finally:
        reader.close()
