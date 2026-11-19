"""Atomic WeChat identity -> Conversation binding tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cairn.conversation.store import DomainStore
from tests.support.pg import database_for


def _bind(store: DomainStore, workspace: Path, account: str, sender: str) -> str:
    return store.get_or_create_wechat_conversation(
        workspace=workspace,
        account_id=account,
        sender_id=sender,
        initial_prompt="first message",
    ).conversation_id


def test_wechat_binding_reuses_identity_and_isolates_accounts_and_senders(
    tmp_path: Path,
) -> None:
    store = DomainStore(database_for(tmp_path))
    first = _bind(store, tmp_path, "bot-1", "owner")
    assert _bind(store, tmp_path, "bot-1", "owner") == first
    assert _bind(store, tmp_path, "bot-1", "other") != first
    assert _bind(store, tmp_path, "bot-2", "owner") != first


def test_wechat_first_message_concurrency_creates_one_conversation(
    tmp_path: Path,
) -> None:
    store = DomainStore(database_for(tmp_path))

    def bind(_: int) -> str:
        return _bind(store, tmp_path, "bot-1", "owner")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(bind, range(2)))
    assert results[0] == results[1]
    assert len(store.list_conversations()) == 1


def test_wechat_binding_verifies_exact_account_and_sender(tmp_path: Path) -> None:
    store = DomainStore(database_for(tmp_path))
    conversation = _bind(store, tmp_path, "bot-1", "owner")

    assert store.is_wechat_binding(conversation, account_id="bot-1", sender_id="owner")
    assert not store.is_wechat_binding(
        conversation, account_id="bot-1", sender_id="other"
    )
    assert not store.is_wechat_binding(
        conversation, account_id="bot-2", sender_id="owner"
    )
    assert not store.is_wechat_binding(
        "conversation-missing", account_id="bot-1", sender_id="owner"
    )
    assert not store.is_wechat_binding(conversation, account_id="", sender_id="owner")
