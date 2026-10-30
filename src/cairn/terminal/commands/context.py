from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cairn.core.model_manager import ModelConfig, ModelManager
from cairn.core.provider_catalog import ProviderCatalog
from cairn.infra.observability.storage import TraceStore
from cairn.runtime.model_executor import ModelExecutor
from cairn.terminal.action import Action

if TYPE_CHECKING:
    from cairn.runtime.turn.compaction import CompactionOutcome


@dataclass
class CommandContext:
    trace_store: TraceStore
    last_trace_id: str | None = None
    model_manager: ModelManager | None = None
    model_executor: ModelExecutor | None = None
    select_model: Callable[[str], ModelConfig] | None = None
    choose_model: (
        Callable[[str, tuple[ModelConfig, ...], str], Awaitable[str | None]] | None
    ) = None
    provider_catalog: ProviderCatalog | None = None
    active_provider_name: str | None = None
    select_provider: Callable[[str], None] | None = None
    #: Asks for provider trust and then switches, when the caller owns an
    #: interactive surface. Commands return it as their interaction so the question
    #: is answered while the loop keeps drawing, rather than blocking it.
    approve_provider: Callable[[str], Awaitable["Action"]] | None = None
    #: Manual context compaction, bound to the live Agent by the CLI. ``None``
    #: when the session has no agent or compaction is switched off.
    compact_now: Callable[[], Awaitable["CompactionOutcome"]] | None = None
    #: Whether this session has a bottom action surface to open a chooser in. The
    #: CLI sets it when it owns a frame; a script has none, so commands answer in
    #: plain text instead of waiting for a keypress that will never come.
    has_action_surface: bool = False
    #: In-session `/wx` controller bound by the CLI; None in non-interactive callers.
    wechat_command: Callable[[str], Awaitable[Action]] | None = None

    def chooser_available(self) -> bool:
        """Whether a chooser can be shown and answered."""
        return self.has_action_surface
