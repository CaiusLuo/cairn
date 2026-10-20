"""Bounded context compaction: which part of the request may become a summary.

Compaction never edits conversation facts. ``AgentState.messages`` stays the
authoritative record of what happened; this package decides which slice of it a
single request still sends verbatim and which already-settled slice it replaces
with a summary. That split is what lets a turn that keeps producing tool output
stay inside its budget without discarding executed facts.

Two rules are load-bearing and live here as pure functions:

* a cut point may never separate an assistant tool call from its tool result, so
  every candidate request stays valid for a provider;
* the current user turn's own message is never summarized, so the instruction
  being worked on is always sent verbatim.
"""

from cairn.core.compaction.budget_values import (
    DEFAULT_CONTEXT_MAX_TOKENS,
    DEFAULT_RESPONSE_MAX_TOKENS,
    ContextBudget,
    TokenCount,
)
from cairn.core.compaction.replacement import (
    SUMMARY_PREFIX,
    ReplacementError,
    apply_replacement,
    folded_placeholder,
    summary_message,
)
from cairn.core.compaction.reuse import ReusableCompaction, StaleCompaction
from cairn.core.compaction.rules import (
    CompactionBoundary,
    CompactionPolicy,
    CompactionRecord,
    CompactionStatus,
    CutPointError,
    RetentionWindow,
    choose_boundary,
    count_groups,
    is_cut_point,
    legal_cut_points,
    summarizer_reserve,
)

__all__ = [
    "DEFAULT_CONTEXT_MAX_TOKENS",
    "DEFAULT_RESPONSE_MAX_TOKENS",
    "SUMMARY_PREFIX",
    "CompactionBoundary",
    "CompactionPolicy",
    "CompactionRecord",
    "CompactionStatus",
    "ContextBudget",
    "CutPointError",
    "ReplacementError",
    "RetentionWindow",
    "ReusableCompaction",
    "StaleCompaction",
    "TokenCount",
    "apply_replacement",
    "choose_boundary",
    "count_groups",
    "folded_placeholder",
    "is_cut_point",
    "legal_cut_points",
    "summarizer_reserve",
    "summary_message",
]
