"""Sandbox and approval Protocols.

SandboxRunner   — executes shell commands in a controlled environment.
ApprovalManager — decides whether a requested tool call is allowed.

All AgentLoop / Tool code depends only on these Protocols, never on
the concrete terminal or subprocess implementations.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from krodo.core.types import ApprovalMode, Decision, ToolCall


@runtime_checkable
class SandboxRunner(Protocol):
    async def run(
        self,
        cmd: list[str],
        cwd: Path,
        timeout: float,
    ) -> tuple[int, str, str]:
        """Execute *cmd* and return (returncode, stdout, stderr)."""
        ...

    def is_path_allowed(self, path: Path) -> bool:
        """Return True iff *path* is inside the permitted workspace root."""
        ...

    def is_command_allowed(self, cmd: list[str]) -> bool:
        """Return True iff the command is not on the blocklist."""
        ...


@runtime_checkable
class ApprovalManager(Protocol):
    mode: ApprovalMode

    async def check(self, tool_call: ToolCall) -> Decision:
        """Decide whether *tool_call* should proceed.

        Decision priority:
        1. Current mode (read_only / auto_edit / full_auto)
        2. tool.requires_approval default value
        3. policy.toml rules (Phase 3+)
        4. Already-trusted session / pattern
        """
        ...

    def trust_session(self, tool_name: str) -> None:
        """Mark *tool_name* as trusted for the remainder of the session."""
        ...
