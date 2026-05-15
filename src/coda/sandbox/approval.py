"""TerminalApprovalManager — interactive y/n/a approval UX for the terminal.

M1 scope: auto_edit mode only.
- read_file:     never requires approval.
- write_file / run_shell: require approval (y once / a = trust for session / n = deny).
- A session-scoped trust set means once you answer 'a', all further write/shell
  calls are auto-approved without prompting.

M2 will add: read_only / full_auto modes, approve_pattern.
"""

from __future__ import annotations

from coda.core.types import ApprovalMode, Decision, ToolCall

# Tools that never need approval in auto_edit mode.
_NO_APPROVAL_TOOLS: frozenset[str] = frozenset(
    {"read_file", "glob", "list_dir", "grep", "git_status", "git_diff"}
)


class TerminalApprovalManager:
    """Approval manager for interactive terminal sessions.

    Parameters
    ----------
    mode:
        Approval mode.  M1 only supports ``"auto_edit"``.
    """

    def __init__(self, mode: ApprovalMode = "auto_edit") -> None:
        self.mode: ApprovalMode = mode
        self._session_trusted: set[str] = set()

    # ------------------------------------------------------------------
    # ApprovalManager Protocol implementation
    # ------------------------------------------------------------------

    async def check(self, tool_call: ToolCall) -> Decision:
        """Decide whether *tool_call* should proceed.

        Returns
        -------
        Decision
            ``"approve"`` / ``"approve_session"`` / ``"deny"``.
        """
        if self.mode == "full_auto":
            return "approve"

        if self.mode == "read_only":
            # Only read-class tools allowed; everything else denied.
            if tool_call.name in _NO_APPROVAL_TOOLS:
                return "approve"
            return "deny"

        # auto_edit mode (default)
        if tool_call.name in _NO_APPROVAL_TOOLS:
            return "approve"

        # Already trusted for this session?
        if tool_call.name in self._session_trusted:
            return "approve_session"

        # Interactive prompt
        return await self._prompt(tool_call)

    def trust_session(self, tool_name: str) -> None:
        """Mark *tool_name* as session-trusted (called when user answers 'a')."""
        self._session_trusted.add(tool_name)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _prompt(self, tool_call: ToolCall) -> Decision:
        """Show a terminal prompt and wait for y/n/a input."""
        import asyncio

        from rich.console import Console

        console = Console()
        path_hint = tool_call.arguments.get("path", "")
        workspace_hint = ""
        try:
            from coda.tools.protocols import ToolContext  # lazy import to avoid circular

            _ = ToolContext  # ensure importable
        except ImportError:
            pass

        summary_parts = [f"[bold yellow]{tool_call.name}[/bold yellow]"]
        if path_hint:
            summary_parts.append(str(path_hint))
        console.print(f"\nApproval requested: {' '.join(summary_parts)}")
        if workspace_hint:
            console.print(f"  workspace={workspace_hint}")

        loop = asyncio.get_event_loop()
        while True:
            choice = await loop.run_in_executor(
                None,
                lambda: (
                    input("  Approve? [y=once / n=deny / a=trust this session]: ").strip().lower()
                ),
            )
            if choice == "y":
                return "approve"
            if choice == "n":
                return "deny"
            if choice == "a":
                self.trust_session(tool_call.name)
                return "approve_session"
            # invalid input — re-prompt
