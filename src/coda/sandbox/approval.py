"""TerminalApprovalManager — interactive approval UX for the terminal.

M2 upgrade: full three-mode support + pattern trust.

Modes
-----
read_only   Read-class tools auto-approved; write/shell always denied.
auto_edit   Read-class tools auto-approved; write/shell prompt y/n/a/p/?.
full_auto   All tools auto-approved; startup warning printed by CLI.

Pattern trust
-------------
The user can answer 'p' to enter a pattern rule such as:
    git_status *              — trust all git_status calls
    run_shell pytest *        — trust shell calls whose cmd starts with 'pytest'
    read_file src/coda/*      — trust read_file calls for paths matching the glob

Patterns are stored in ``_pattern_trust: list[PatternRule]`` in memory
(SQLite persistence is deferred to M5).

Prompt options
--------------
y  approve once
n  deny
a  trust this tool for the rest of the session
p  enter a pattern rule
?  print help
"""

from __future__ import annotations

import asyncio
import fnmatch
from dataclasses import dataclass
from typing import Any

from coda.core.types import ApprovalMode, Decision, ToolCall

# Tools that are always safe to auto-approve in auto_edit / full_auto mode.
_NO_APPROVAL_TOOLS: frozenset[str] = frozenset(
    {
        "read_file",
        "glob",
        "list_dir",
        "grep",
        "git_status",
        "git_diff",
    }
)

_PROMPT_HELP = """\
  y  approve this call once
  n  deny this call
  a  approve all future calls from this tool in this session
  p  enter a pattern rule to approve matching calls automatically
  ?  show this help
Pattern rule syntax:
  <tool_name> <glob>   e.g. "git_status *" or "run_shell pytest *"
"""


@dataclass
class PatternRule:
    tool_name: str  # exact tool name, e.g. "run_shell"
    arg_glob: str  # glob matched against the first argument value (or "*" for all)


def _match_pattern(tool_call: ToolCall, rules: list[PatternRule]) -> bool:
    """Return True if *tool_call* matches any stored pattern rule."""
    for rule in rules:
        if tool_call.name != rule.tool_name:
            continue
        if rule.arg_glob == "*":
            return True
        # Match the glob against the first string argument value (path, cmd, …)
        first_arg = _first_str_arg(tool_call.arguments)
        if first_arg is not None and fnmatch.fnmatch(first_arg, rule.arg_glob):
            return True
    return False


def _first_str_arg(args: dict[str, Any]) -> str | None:
    """Return the first string-valued argument, or None."""
    for v in args.values():
        if isinstance(v, str):
            return v
    return None


class TerminalApprovalManager:
    """Approval manager for interactive terminal sessions.

    Parameters
    ----------
    mode:
        Approval mode: ``"read_only"``, ``"auto_edit"`` (default), or
        ``"full_auto"``.
    """

    def __init__(self, mode: ApprovalMode = "auto_edit") -> None:
        self.mode: ApprovalMode = mode
        self._session_trusted: set[str] = set()
        self._pattern_trust: list[PatternRule] = []

    # ------------------------------------------------------------------
    # ApprovalManager Protocol implementation
    # ------------------------------------------------------------------

    async def check(self, tool_call: ToolCall) -> Decision:
        """Decide whether *tool_call* should proceed."""
        if self.mode == "full_auto":
            return "approve"

        # Read-class tools are safe regardless of mode
        if tool_call.name in _NO_APPROVAL_TOOLS:
            return "approve"

        if self.mode == "read_only":
            # Write/command tools are always denied in read_only
            return "deny"

        # auto_edit mode: check session trust + pattern trust
        if tool_call.name in self._session_trusted:
            return "approve_session"

        if _match_pattern(tool_call, self._pattern_trust):
            return "approve_pattern"

        # Interactive prompt
        return await self._prompt(tool_call)

    def trust_session(self, tool_name: str) -> None:
        """Mark *tool_name* as session-trusted (called when user answers 'a')."""
        self._session_trusted.add(tool_name)

    def add_pattern_rule(self, rule: PatternRule) -> None:
        """Register a new pattern rule (called when user answers 'p')."""
        self._pattern_trust.append(rule)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _prompt(self, tool_call: ToolCall) -> Decision:
        """Show terminal prompt and wait for y/n/a/p/? input."""
        from rich.console import Console

        console = Console()
        path_hint = tool_call.arguments.get("path", "")
        cmd_hint = tool_call.arguments.get("cmd", "")

        summary_parts: list[str] = [f"[bold yellow]{tool_call.name}[/bold yellow]"]
        if path_hint:
            summary_parts.append(str(path_hint))
        elif cmd_hint:
            summary_parts.append(str(cmd_hint))

        console.print(f"\nApproval requested: {' '.join(summary_parts)}")

        loop = asyncio.get_event_loop()
        while True:
            choice = await loop.run_in_executor(
                None,
                lambda: (
                    input("  Approve? [y=once / n=deny / a=session / p=pattern / ?=help]: ")
                    .strip()
                    .lower()
                ),
            )
            if choice == "y":
                return "approve"
            if choice == "n":
                return "deny"
            if choice == "a":
                self.trust_session(tool_call.name)
                return "approve_session"
            if choice == "p":
                rule = await self._prompt_pattern(tool_call, loop)
                if rule is not None:
                    self.add_pattern_rule(rule)
                    return "approve_pattern"
                # invalid pattern input — re-prompt
            elif choice == "?":
                console.print(_PROMPT_HELP)
            # invalid choice — re-prompt

    async def _prompt_pattern(
        self, tool_call: ToolCall, loop: asyncio.AbstractEventLoop
    ) -> PatternRule | None:
        """Ask the user to enter a pattern rule; return None on invalid input."""
        from rich.console import Console

        console = Console()
        console.print(
            f"  Enter pattern (e.g. '{tool_call.name} *' or '{tool_call.name} src/*'): ",
            end="",
        )
        raw = await loop.run_in_executor(None, input)
        parts = raw.strip().split(None, 1)
        if len(parts) < 1 or not parts[0]:
            console.print("  [red]Invalid pattern — must be '<tool_name> <glob>'[/red]")
            return None
        tool_name = parts[0]
        arg_glob = parts[1] if len(parts) > 1 else "*"
        return PatternRule(tool_name=tool_name, arg_glob=arg_glob)
