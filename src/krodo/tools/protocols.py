"""Tool Protocol and ToolContext — the sole abstractions that AgentLoop uses
when dispatching tool calls.

ToolContext is passed to every tool execution so that tools can access the
workspace root, sandbox runner, and session logger without holding global
state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from krodo.core.types import ToolDef, ToolResult

if TYPE_CHECKING:
    import logging

    from krodo.core.workspace import Workspace
    from krodo.sandbox.checkpoint import GitCheckpointManager
    from krodo.sandbox.ignore import KrodoIgnore
    from krodo.sandbox.protocols import SandboxRunner


def _default_ignore() -> KrodoIgnore:
    """Lazy import avoids circular dependency at module load time."""
    # Fallback: ignore nothing (used when ToolContext is created without a real
    # workspace, e.g. in older tests that haven't been updated yet).
    import tempfile
    from pathlib import Path

    from krodo.sandbox.ignore import KrodoIgnore

    tmp = Path(tempfile.mkdtemp())
    return KrodoIgnore(tmp)


def _default_checkpoint() -> GitCheckpointManager:
    """Return a no-op checkpoint manager for tests that don't wire one up."""
    import tempfile
    from datetime import UTC, datetime
    from pathlib import Path

    from krodo.core.workspace import Workspace
    from krodo.sandbox.checkpoint import GitCheckpointManager

    tmp = Path(tempfile.mkdtemp())
    ws = Workspace(
        root=tmp,
        config_path=tmp / ".krodo" / "config.yaml",
        memory_paths=[],
        git_root=None,
        source="cwd",
        discovered_at=datetime.now(tz=UTC),
    )
    return GitCheckpointManager(ws)


@dataclass
class ToolContext:
    """Injected into every tool execution.  Tools must not call Path.cwd()."""

    workspace: Workspace
    sandbox: SandboxRunner
    session_id: str
    logger: logging.Logger
    ignore: KrodoIgnore = field(default_factory=_default_ignore)
    checkpoint: GitCheckpointManager = field(default_factory=_default_checkpoint)
    event_logger: object | None = None  # SessionEventLogger injected by CLI


@runtime_checkable
class Tool(Protocol):
    definition: ToolDef
    requires_approval: bool  # Default; final decision made by ApprovalManager.check()

    async def execute(self, args: dict[str, object], ctx: ToolContext) -> ToolResult:
        """Run the tool and return a structured result.  Never raise — wrap errors."""
        ...
