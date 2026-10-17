"""Public tool contracts and default assembly.

Import concrete implementations from their owning modules. Permission policy
lives in core; runtime owns preflight, approval and execution authority.
"""

from cairn.infra.tools.base import (
    InvalidArguments,
    Tool,
    ToolExecutionContext,
    ToolExecutionFailure,
    ToolFailureKind,
    ToolFailureOutcome,
    ToolNotFound,
)
from cairn.infra.tools.defaults import build_default_registry
from cairn.infra.tools.registry import ToolRegistry

__all__ = [
    "InvalidArguments",
    "Tool",
    "ToolExecutionContext",
    "ToolExecutionFailure",
    "ToolFailureKind",
    "ToolFailureOutcome",
    "ToolNotFound",
    "ToolRegistry",
    "build_default_registry",
]
