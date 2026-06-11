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
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from coda.core.workspace import LocalWorkspaceResolver
from coda.memory.replay import replay_events
from coda.memory.store import JsonlSessionStore

if TYPE_CHECKING:
    from coda.cli.main import SessionComponents

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

        console.print(render_sessions_table(rows))
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

    async def _entry() -> None:
        from coda.cli.main import _build_session_components  # noqa: PLC0415

        def _rebuild(target_id: str) -> SessionComponents:
            return _build_session_components(
                root=root,
                model=model,
                api_key=api_key,
                api_base=api_base,
                approval_mode=approval,
                max_tool_calls=max_tool_calls,
                max_tokens=max_tokens,
                resume_session_id=target_id,
            )

        components = build_resumed_components(resolved_id, _rebuild)
        await repl_session_cycle(components, _rebuild)

    asyncio.run(_entry())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def render_sessions_table(rows: list) -> object:
    """Build the Rich table of recent sessions (shared with the REPL ``:sessions``)."""
    from rich.table import Table  # noqa: PLC0415

    table = Table(title="Recent sessions", show_lines=False)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Created", style="green")
    table.add_column("Last updated", style="green")
    table.add_column("Model", style="dim")
    for row in rows:
        created = row.created_at.strftime("%Y-%m-%d %H:%M")
        updated = row.last_updated_at.strftime("%Y-%m-%d %H:%M")
        table.add_row(row.session_id, created, updated, row.model or "—")
    return table


def build_resumed_components(
    session_id: str,
    rebuild: Callable[[str], SessionComponents],
) -> SessionComponents:
    """Build components for *session_id* and replay its event history."""
    components = rebuild(session_id)
    events = components.store.load_events(session_id)

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
            components.workspace.root,
        )

    return components


async def repl_session_cycle(
    components: SessionComponents,
    rebuild: Callable[[str], SessionComponents],
) -> None:
    """Run the REPL, rebuilding components whenever ``:resume`` switches session.

    ``run_repl`` returns the target session id when the user issued
    ``:resume <id>`` (or None on normal exit).  Each switch rebuilds the
    full component bundle for the target session and replays its history,
    so the conversation continues exactly where that session left off.
    """
    from coda.cli.repl import run_repl  # noqa: PLC0415

    current = components
    while True:
        target = await run_repl(current)
        if target is None:
            return
        current = build_resumed_components(target, rebuild)


def _print_conversation_history(
    history: list,
    workspace_root: Path | None = None,
) -> None:
    """Print a compact summary of the replayed conversation to stderr.

    Each message is expanded to one or more display entries by
    ``_history_entries``:
      * user message          -> one "text" entry
      * assistant text reply  -> one "text" entry
      * assistant tool calls  -> one "tool" entry per message
      * assistant with BOTH   -> "text" entry then "tool" entry

    Consecutive "tool" entries across the flat list are grouped; only the
    first ``max_tool_lines_per_run`` are shown, the tail folds into a
    ``... +k more tool calls`` line.

    The display window is anchored on *user turns* so the most recent
    prompts stay visible even when a turn produced many tool-call messages.
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

    # Flatten messages into (kind, rendered_line, weight) entries.
    # kind:   "text" for narration/user lines, "tool" for [called X] lines.
    # weight: number of actual tool calls in this entry (used for fold count).
    entries: list[tuple[str, str, int]] = []
    for msg in show:
        entries.extend(_history_entries(msg, assistant_truncate, workspace_root))

    if not entries:
        return

    console = Console(stderr=True)
    console.print("[dim]" + "\u2500" * 3 + " previous conversation " + "\u2500" * 27 + "[/dim]")
    if start > 0:
        console.print(
            f"[dim]  showing last {max_user_turns} turn(s) "
            f"of {len(user_indices)} ({len(visible)} messages total)[/dim]"
        )

    # Walk the flat entry list, folding consecutive "tool" runs.
    i = 0
    while i < len(entries):
        kind, line, weight = entries[i]
        if kind == "tool":
            # Collect the full consecutive run of tool entries.
            run = [(line, weight)]
            j = i + 1
            while j < len(entries) and entries[j][0] == "tool":
                run.append((entries[j][1], entries[j][2]))
                j += 1

            for ln, _ in run[:max_tool_lines_per_run]:
                console.print(ln)
            tail = run[max_tool_lines_per_run:]
            if tail:
                extra = sum(w or 1 for _, w in tail)
                console.print(f"[dim]          \u2026 +{extra} more tool calls[/dim]")
            i = j
        else:
            console.print(line)
            i += 1


def _history_entries(
    msg: object,
    truncate: int,
    workspace_root: Path | None = None,
) -> list[tuple[str, str, int]]:
    """Expand one history message into ``(kind, line, weight)`` display entries.

    A message that carries both narration text and tool calls yields two
    entries: the "text" entry first (narration), then the "tool" entry
    (``[called X]`` summary).  This ensures both are visible on resume.
    """
    role = getattr(msg, "role", None)
    raw_content = getattr(msg, "content", None)
    content = (raw_content if isinstance(raw_content, str) else "").strip()
    tool_calls = getattr(msg, "tool_calls", None) or []

    entries: list[tuple[str, str, int]] = []

    if role == "user":
        return [("text", f"[dim] you   {content}[/dim]", 0)]

    # assistant
    if content:
        if len(content) > truncate:
            content = content[: truncate - 3] + "..."
        entries.append(("text", f"[dim] asst  {content}[/dim]", 0))

    if tool_calls:
        summaries = [
            _tool_call_summary(tc, workspace_root)
            for tc in tool_calls
            if getattr(tc, "name", "")
        ]
        names = ", ".join(summaries)
        if names:
            # Escape "[" so Rich does not treat it as markup.
            line = f"[dim] asst  \\[called {names}][/dim]"
            entries.append(("tool", line, len(tool_calls)))

    # Empty content and no tool calls — skip (defensive).
    return entries


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
