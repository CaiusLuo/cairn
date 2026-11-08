"""Atomic WeChat identity -> Conversation binding tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cairn.infra.domain_store import DomainStore


def _bind(store: DomainStore, account: str, sender: str) -> str:
    return store.get_or_create_wechat_conversation(
        workspace=store.path.parent,
        account_id=account,
        sender_id=sender,
        initial_prompt="first message",
    ).conversation_id


def test_wechat_binding_reuses_identity_and_isolates_accounts_and_senders(
    tmp_path: Path,
) -> None:
    store = DomainStore(tmp_path / "domain.sqlite3")
    first = _bind(store, "bot-1", "owner")
    assert _bind(store, "bot-1", "owner") == first
    assert _bind(store, "bot-1", "other") != first
    assert _bind(store, "bot-2", "owner") != first


def test_wechat_first_message_concurrency_creates_one_conversation(
    tmp_path: Path,
) -> None:
    store = DomainStore(tmp_path / "domain.sqlite3")

    def bind(_: int) -> str:
        return _bind(store, "bot-1", "owner")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(bind, range(2)))
    assert results[0] == results[1]
    assert len(store.list_conversations()) == 1
