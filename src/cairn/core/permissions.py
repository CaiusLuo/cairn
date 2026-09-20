from enum import Enum
from typing import Protocol

from cairn.core.models import ToolCall

class PermissionDecision(str ,Enum):
    ALLOW = "allow"
    DENY = "deny"

class PermissionHandler(Protocol):
    def __call__(
            self,
            tool_call: ToolCall,
    ) -> PermissionDecision:
        ...

