"""coda resume — reload a previous session into an interactive REPL (M5.2).

Usage::

    coda resume                   # resume the most recent session in cwd workspace
    coda resume <session_id>      # resume a specific session by full or prefix ID
    coda resume --list            # list the 10 most recent sessions

The resumed session's conversation history is reconstructed from the
``SessionStore`` event log via ``replay_events``, then the interactive REPL
starts from there.  The user can continue the conversation exactly as if the
process had never exited.

All standard flags (``--model``, ``--api-key``, ``--approval``, etc.) are
available and override the model used in the original session.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from coda.core.workspace import LocalWorkspaceResolver
from coda.memory.replay import replay_events
from coda.memory.store import JsonlSessionStore

if TYPE_CHECKING:
    pass

_DEFAULT_MODEL = "anthropic/claude-3-5-sonnet-20241022"


def resume_command(
    session_id: str | None = None,
    *,
    root: Path | None = None,
    model: str = _DEFAULT_MODEL,
    api_key: str | None = None,
    api_base: str | None = None,
    approval: str = "auto_edit",
    max_tool_calls: int = 15,
    max_tokens: int = 16384,
    list_recent: bool = False,
    _workspace_root: Path | None = None,  # test-only injection
) -> None:
    """Resume or list sessions for the current workspace."""
    from rich.console import Console  # noqa: PLC0415
    from rich.table import Table  # noqa: PLC0415

    console = Console()

    # Resolve workspace
    if _workspace_root is not None:
        workspace_root = _workspace_root
        resolver = LocalWorkspaceResolver()
        workspace = resolver.resolve(explicit=workspace_root)
    else:
        resolver = LocalWorkspaceResolver()
        workspace = resolver.resolve(explicit=root)
        workspace_root = workspace.root

    store = JsonlSessionStore(workspace_root / ".coda" / "sessions")

    # --list: print recent sessions and exit
    if list_recent:
        rows = store.list_recent(limit=10)
        if not rows:
            console.print("[dim]No sessions found in this workspace.[/dim]")
            raise typer.Exit(code=0)

        table = Table(title="Recent sessions", show_lines=False)
        table.add_column("ID", style="cyan", no_wrap=True)
        table.add_column("Created", style="green")
        table.add_column("Last updated", style="green")
        table.add_column("Model", style="dim")
        for row in rows:
            created = row.created_at.strftime("%Y-%m-%d %H:%M")
            updated = row.last_updated_at.strftime("%Y-%m-%d %H:%M")
            table.add_row(row.session_id, created, updated, row.model or "—")
        console.print(table)
        raise typer.Exit(code=0)

    # Resolve session ID (exact match or prefix match)
    resolved_id = _resolve_session_id(store, session_id)
    if resolved_id is None:
        if session_id:
            console.print(
                f"[red]No session matching '{session_id}' in {workspace_root}/.coda/sessions/[/red]"
            )
        else:
            console.print(
                f"[red]No sessions found in {workspace_root}/.coda/sessions/. "
                "Did you run coda in this workspace?[/red]"
            )
        raise typer.Exit(code=1)

    # Load events and build components with resumed session ID
    events = store.load_events(resolved_id)

    async def _entry() -> None:
        from coda.cli.main import _build_session_components  # noqa: PLC0415
        from coda.cli.repl import run_repl  # noqa: PLC0415

        components = _build_session_components(
            root=root,
            model=model,
            api_key=api_key,
            api_base=api_base,
            approval_mode=approval,
            max_tool_calls=max_tool_calls,
            max_tokens=max_tokens,
            resume_session_id=resolved_id,
        )

        # Replay the stored events into the context manager
        stats = replay_events(events, components.loop.context_manager)
        if stats.messages_restored > 0:
            from rich.console import Console as _RichConsole  # noqa: N814, PLC0415

            note = "[yellow](compressed)[/yellow] " if stats.compressed else ""
            _RichConsole(stderr=True).print(
                f"[dim]Replayed {stats.messages_restored} messages, "
                f"{stats.turns} user turn(s). {note}Starting REPL…[/dim]"
            )

            _print_conversation_history(
                components.loop.context_manager.history,
                workspace_root,
            )

        await run_repl(components)

    asyncio.run(_entry())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _print_conversation_history(
    history: list,
    workspace_root: Path | None = None,
) -> None:
    """Print a compact summary of the replayed conversation to stderr.

    Assistant messages come in two shapes in a ReAct loop:
      * tool-call messages — ``content`` is empty, ``tool_calls`` is populated;
        rendered as ``[called <tool> <arg>]``.
      * text replies — ``content`` has the natural-language answer.

    The display window is anchored on *user turns* (not raw message count) so
    that the most recent user prompts stay visible even when a single turn
    produced many tool-call messages.

    Consecutive tool-call assistant messages are grouped into runs.  Only the
    first ``max_tool_lines_per_run`` lines of each run are shown; the tail is
    folded into a single ``... +k more tool calls`` line.
    """
    from rich.console import Console  # noqa: PLC0415

    max_user_turns = 3
    assistant_truncate = 120
    max_tool_lines_per_run = 5

    # Filter to user + assistant messages only (tool-result rows are noise here).
    visible = [m for m in history if m.role in ("user", "assistant")]
    if not visible:
        return

    # Anchor the window on the most recent ``max_user_turns`` user messages so
    # the "you" prompts are never pushed out by a long tool-call sequence.
    user_indices = [i for i, m in enumerate(visible) if m.role == "user"]
    if len(user_indices) > max_user_turns:
        start = user_indices[-max_user_turns]
    else:
        start = 0
    show = visible[start:]

    console = Console(stderr=True)
    console.print("[dim]" + "\u2500" * 3 + " previous conversation " + "\u2500" * 27 + "[/dim]")
    if start > 0:
        console.print(
            f"[dim]  showing last {max_user_turns} turn(s) "
            f"of {len(user_indices)} ({len(visible)} messages total)[/dim]"
        )

    # Walk ``show`` grouping consecutive tool-call messages into runs.
    i = 0
    while i < len(show):
        msg = show[i]
        if _is_tool_call_msg(msg):
            # Collect the full run of consecutive tool-call messages.
            run = [msg]
            j = i + 1
            while j < len(show) and _is_tool_call_msg(show[j]):
                run.append(show[j])
                j += 1

            # Print up to max_tool_lines_per_run lines, then fold the rest.
            for m in run[:max_tool_lines_per_run]:
                line = _format_history_line(m, assistant_truncate, workspace_root)
                if line is not None:
                    console.print(line)
            tail = run[max_tool_lines_per_run:]
            if tail:
                extra = sum(
                    len(getattr(m, "tool_calls", None) or []) or 1 for m in tail
                )
                console.print(f"[dim]          \u2026 +{extra} more tool calls[/dim]")
            i = j
        else:
            line = _format_history_line(msg, assistant_truncate, workspace_root)
            if line is not None:
                console.print(line)
            i += 1


def _is_tool_call_msg(msg: object) -> bool:
    """Return True when *msg* is an assistant message whose content is empty and
    ``tool_calls`` is non-empty (i.e. a ReAct tool-invocation step)."""
    if getattr(msg, "role", None) != "assistant":
        return False
    raw = getattr(msg, "content", None)
    content = raw if isinstance(raw, str) else ""
    if content.strip():
        return False
    return bool(getattr(msg, "tool_calls", None))


def _format_history_line(
    msg: object,
    truncate: int,
    workspace_root: Path | None = None,
) -> str | None:
    """Render one history message as a dim stderr line, or None to skip it."""
    role = getattr(msg, "role", None)
    raw_content = getattr(msg, "content", None)
    content = raw_content if isinstance(raw_content, str) else ""
    content = content.strip()

    if role == "user":
        return f"[dim] you   {content}[/dim]"

    # assistant text reply
    if content:
        if len(content) > truncate:
            content = content[: truncate - 3] + "..."
        return f"[dim] asst  {content}[/dim]"

    # No text content — summarise the tool calls instead of printing a blank.
    tool_calls = getattr(msg, "tool_calls", None)
    if tool_calls:
        summaries = [
            _tool_call_summary(tc, workspace_root)
            for tc in tool_calls
            if getattr(tc, "name", "")
        ]
        names = ", ".join(summaries)
        if names:
            # Escape the opening bracket so Rich renders it literally instead of
            # treating "[called ...]" as console markup (which would blank it out).
            return f"[dim] asst  \\[called {names}][/dim]"
    # Empty content and no tool calls — defensive skip.
    return None


def _tool_call_summary(tc: object, workspace_root: Path | None) -> str:
    """Return ``"<name> <key-arg>"`` for a tool call, or just ``"<name>"``."""
    name = getattr(tc, "name", "") or ""
    arg = _key_arg(getattr(tc, "arguments", None), workspace_root)
    return f"{name} {arg}" if arg else name


_PATH_KEYS = frozenset({"path", "file_path", "file", "filename", "target", "source"})
_OTHER_KEYS = frozenset({"pattern", "query", "command", "cmd", "url", "text"})
_ARG_TRUNCATE = 40


def _key_arg(arguments: object, workspace_root: Path | None) -> str:
    """Extract the most informative single argument string from a tool-call dict.

    Priority:
    1. Path-ish keys — rendered relative to *workspace_root* when possible.
    2. Other common informational keys (pattern, query, command, …).
    3. First string value in the dict as a fallback.

    Returns an empty string when nothing useful can be extracted.
    """
    if not isinstance(arguments, dict) or not arguments:
        return ""

    def _render_path(raw: object) -> str:
        val = str(raw) if not isinstance(raw, str) else raw
        if not val:
            return ""
        if workspace_root is not None:
            try:
                rel = Path(val).relative_to(workspace_root)
                val = str(rel)
            except ValueError:
                pass
        return _truncate(val)

    for key in _PATH_KEYS:
        if key in arguments:
            return _render_path(arguments[key])

    for key in _OTHER_KEYS:
        if key in arguments:
            return _truncate(str(arguments[key]))

    # Fallback: first string value
    for v in arguments.values():
        if isinstance(v, str) and v:
            return _truncate(v)

    return ""


def _truncate(s: str) -> str:
    return s if len(s) <= _ARG_TRUNCATE else s[: _ARG_TRUNCATE - 3] + "..."


def _resolve_session_id(
    store: JsonlSessionStore,
    session_id: str | None,
) -> str | None:
    """Resolve *session_id* to a full ID in the store.

    - ``None`` → most recent session (``list_recent(limit=1)[0]``).
    - Exact match → return as-is if the file exists.
    - Prefix match → resolve if exactly one session starts with the prefix.
    - Multiple prefix matches → fail loudly.
    """
    if session_id is None:
        rows = store.list_recent(limit=1)
        return rows[0].session_id if rows else None

    # Try exact match first
    sessions_dir = store._dir  # noqa: SLF001
    exact = sessions_dir / f"{session_id}.jsonl"
    if exact.exists():
        return session_id

    # Prefix match
    all_rows = store.list_recent(limit=1000)
    matches = [r.session_id for r in all_rows if r.session_id.startswith(session_id)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        from rich.console import Console  # noqa: PLC0415

        Console().print(
            f"[red]Ambiguous session prefix '{session_id}'. "
            f"Matches: {', '.join(matches[:5])}[/red]"
        )
        raise typer.Exit(code=1)

    return None


# ---------------------------------------------------------------------------
# Typer registration
# ---------------------------------------------------------------------------


def register_resume_app(app: typer.Typer) -> None:
    """Register ``coda resume`` as a subcommand."""
    resume_sub = typer.Typer(
        name="resume",
        help="Resume a previous Coda session.",
        add_completion=False,
        invoke_without_command=True,
    )

    @resume_sub.callback(invoke_without_command=True)
    def _resume(
        session_id: str | None = typer.Argument(
            None,
            help="Session ID to resume (full or prefix). Default: most recent.",
        ),
        root: Path | None = typer.Option(
            None,
            "--root",
            "-r",
            help="Workspace root (default: auto-discover from cwd)",
            envvar="CODA_ROOT",
        ),
        model: str = typer.Option(
            _DEFAULT_MODEL,
            "--model",
            "-m",
            help="LiteLLM model string (overrides the original session's model)",
            envvar="CODA_MODEL",
        ),
        api_key: str | None = typer.Option(
            None,
            "--api-key",
            help="LLM API key",
            envvar="CODA_API_KEY",
        ),
        api_base: str | None = typer.Option(
            None,
            "--api-base",
            help="Custom LLM API base URL",
            envvar="CODA_API_BASE",
        ),
        approval: str = typer.Option(
            "auto_edit",
            "--approval",
            "-a",
            help="Approval mode: read_only | auto_edit | full_auto",
            envvar="CODA_APPROVAL",
        ),
        max_tool_calls: int = typer.Option(
            15,
            "--max-tool-calls",
            help="Maximum tool calls per turn",
        ),
        max_tokens: int = typer.Option(
            16384,
            "--max-tokens",
            help="Maximum output tokens per LLM response",
            envvar="CODA_MAX_TOKENS",
        ),
        list_sessions: bool = typer.Option(
            False,
            "--list",
            "-l",
            help="List the 10 most recent sessions in this workspace",
        ),
    ) -> None:
        """Resume a previous Coda session in the current workspace."""
        resume_command(
            session_id=session_id,
            root=root,
            model=model,
            api_key=api_key,
            api_base=api_base,
            approval=approval,
            max_tool_calls=max_tool_calls,
            max_tokens=max_tokens,
            list_recent=list_sessions,
        )

    app.add_typer(resume_sub)
