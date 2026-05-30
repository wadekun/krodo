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

        await run_repl(components)

    asyncio.run(_entry())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
