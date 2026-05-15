"""Tool Protocol and ToolContext — the sole abstractions that AgentLoop uses
when dispatching tool calls.

ToolContext is passed to every tool execution so that tools can access the
workspace root, sandbox runner, and session logger without holding global
state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from coda.core.types import ToolDef, ToolResult

if TYPE_CHECKING:
    import logging

    from coda.core.workspace import Workspace
    from coda.sandbox.protocols import SandboxRunner


@dataclass
class ToolContext:
    """Injected into every tool execution.  Tools must not call Path.cwd()."""

    workspace: Workspace
    sandbox: SandboxRunner
    session_id: str
    logger: logging.Logger


@runtime_checkable
class Tool(Protocol):
    definition: ToolDef
    requires_approval: bool  # Default; final decision made by ApprovalManager.check()

    async def execute(self, args: dict[str, object], ctx: ToolContext) -> ToolResult:
        """Run the tool and return a structured result.  Never raise — wrap errors."""
        ...
