"""Coda interactive REPL.

Multi-turn loop: read a line from stdin, hand it to the shared `AgentLoop`,
print the result, repeat.  Conversation history survives across turns because
the *same* `AgentLoop.context_manager` is reused (see
[coda.core.loop.AgentLoop] docstring).

On a real TTY the input is handled by **prompt_toolkit**, which provides:
  - Left / right arrow key cursor movement
  - Correct backspace / delete including CJK double-width characters
  - Up / down arrow recall of prompts entered in the current session

On non-TTY stdin (CI, pipes, Typer's CliRunner in tests) the implementation
falls back to plain ``input()`` so scripted input still works.

Slash commands (M6.4) — handled locally, never sent to the LLM:
    :help                 list available commands
    :sessions             list recent sessions in this workspace
    :undo                 restore files to the previous checkpoint
    :cost                 show session token/cost totals
    :resume <id>          switch to another session (history is replayed)

Multi-line input remains future work.

Exit conditions:
    - typing one of: ``exit`` / ``quit`` / ``:q`` / ``\\q``
    - Ctrl-D (EOF) — standard Unix REPL exit
    - Ctrl-C pressed twice in a row at an empty prompt
      (single Ctrl-C only resets the "armed" flag, mirroring
      Python/IPython behaviour to avoid accidental exit)
    - Ctrl-C while a turn is running cancels just that turn

The session summary is printed exactly once on exit (not per-turn) so
the REPL feels conversational rather than transactional.
"""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING

from rich.console import Console

if TYPE_CHECKING:
    from coda.cli.main import SessionComponents

_EXIT_TOKENS = {"exit", "quit", ":q", "\\q"}

_console = Console()


async def run_repl(components: SessionComponents) -> str | None:
    """Drive the interactive multi-turn loop until the user exits.

    `components` is shared across every turn so that:
      - `AgentLoop.context_manager` accumulates conversation history,
      - the `SessionEventLogger` keeps appending to one JSONL file,
      - the `GitCheckpointManager` accumulates checkpoints,
      - `session_id` stays stable for the whole REPL lifetime.

    Returns the target session id when the user issued ``:resume <id>``
    (the caller rebuilds components and re-enters), or None on normal exit.
    """
    from prompt_toolkit import PromptSession  # noqa: PLC0415
    from prompt_toolkit.history import InMemoryHistory  # noqa: PLC0415

    _console.print(
        "[dim]REPL mode. Type a prompt, 'exit' / Ctrl-D to quit. "
        "Press Ctrl-C twice to force exit.[/dim]\n"
    )

    # One PromptSession for the whole REPL so up/down history recall works
    # across turns.  Only used when stdin is a real TTY.
    pt_session: PromptSession[str] = PromptSession(history=InMemoryHistory())

    turn_idx = 0
    last_ctrl_c = False
    switch_target: str | None = None

    while True:
        # ----------------------------------------------------------------
        # Read one line from the user.
        # ----------------------------------------------------------------
        try:
            if sys.stdin.isatty():
                # prompt_toolkit: proper cursor movement, backspace/delete,
                # CJK double-width character handling, and in-session history.
                user_input = await pt_session.prompt_async("you> ")
            else:
                # Non-TTY (tests, pipes): plain input() so scripted stdin works.
                user_input = await asyncio.to_thread(input, "you> ")
        except EOFError:
            _console.print("\n[dim]bye.[/dim]")
            break
        except KeyboardInterrupt:
            if last_ctrl_c:
                _console.print("\n[dim]bye.[/dim]")
                break
            _console.print("\n[yellow]Press Ctrl-C again to exit.[/yellow]")
            last_ctrl_c = True
            continue

        # Any successful input clears the "armed Ctrl-C" sentinel.
        last_ctrl_c = False

        stripped = user_input.strip()
        if not stripped:
            continue
        if stripped.lower() in _EXIT_TOKENS:
            break

        # M6.4: slash commands are handled locally — the LLM never sees them.
        # (Checked after exit tokens so ':q' keeps working as an exit.)
        if stripped.startswith(":"):
            switch_target = _dispatch_slash(stripped, components)
            if switch_target is not None:
                break
            continue

        # ----------------------------------------------------------------
        # Run one agent turn.  Ctrl-C inside the turn cancels just this
        # turn (we catch it and continue, instead of letting it tear down
        # the whole REPL).
        # ----------------------------------------------------------------
        turn_idx += 1
        try:
            result = await components.loop.run(stripped)
        except KeyboardInterrupt:
            _console.print("[yellow]Turn cancelled.[/yellow]")
            continue
        except Exception as exc:  # noqa: BLE001
            # A turn-level uncaught exception should not kill the REPL —
            # surface it and let the user try again.
            _console.print(f"[red]Turn failed: {exc}[/red]")
            components.logger.exception("repl_turn_uncaught_error")
            continue

        # Imported lazily to avoid an import cycle (main -> repl -> main).
        from coda.cli.main import _echo_turn_result  # noqa: PLC0415

        _echo_turn_result(result)

        # ----------------------------------------------------------------
        # Tool-call limit hit but the task may be unfinished: offer to
        # continue the SAME turn with a fresh budget.  `continue_turn()`
        # does not inject a new user message — the model sees the
        # synthesized "[skipped: tool call limit reached]" result and
        # re-issues the pending work.  (Headless keeps the hard stop.)
        # ----------------------------------------------------------------
        while result.hit_tool_call_limit:
            prompt_text = "Tool call limit reached — continue? [y/n] "
            try:
                if sys.stdin.isatty():
                    answer = await pt_session.prompt_async(prompt_text)
                else:
                    answer = await asyncio.to_thread(input, prompt_text)
            except (EOFError, KeyboardInterrupt):
                _console.print("[yellow]Stopping this turn.[/yellow]")
                break
            if answer.strip().lower() not in ("y", "yes"):
                break
            try:
                result = await components.loop.continue_turn()
            except KeyboardInterrupt:
                _console.print("[yellow]Turn cancelled.[/yellow]")
                break
            except Exception as exc:  # noqa: BLE001
                _console.print(f"[red]Turn failed: {exc}[/red]")
                components.logger.exception("repl_continue_uncaught_error")
                break
            _echo_turn_result(result)

    # ------------------------------------------------------------------
    # Session switch (`:resume <id>`): hand the target back to the caller,
    # which rebuilds components and re-enters run_repl.  No summary here —
    # the conversation continues in the resumed session.
    # ------------------------------------------------------------------
    if switch_target is not None:
        _console.print(f"[dim]Switching to session {switch_target}…[/dim]")
        components.logger.info(
            "repl_switch_session from=%s to=%s",
            components.session_id,
            switch_target,
        )
        return switch_target

    # ------------------------------------------------------------------
    # Goodbye: print one consolidated session summary.
    # ------------------------------------------------------------------
    from coda.cli.main import print_session_summary  # noqa: PLC0415

    print_session_summary(components, turns=turn_idx)
    components.logger.info(
        "repl_end session_id=%s turns=%d",
        components.session_id,
        turn_idx,
    )
    return None


# ---------------------------------------------------------------------------
# Slash command dispatch (M6.4)
# ---------------------------------------------------------------------------

_SLASH_HELP = """\
[bold]REPL commands[/bold]
  :help            show this help
  :sessions        list recent sessions in this workspace
  :undo            restore files to the previous checkpoint
  :cost            show session token / cost totals
  :resume <id>     switch to another session (full or prefix id)
  :q / exit        quit the REPL"""


def _dispatch_slash(command: str, components: SessionComponents) -> str | None:
    """Execute one slash *command*.

    Returns the resolved target session id for ``:resume <id>`` (the REPL
    then breaks out so the caller can rebuild components), or None when the
    REPL should keep going.
    """
    parts = command.split()
    cmd, args = parts[0].lower(), parts[1:]

    if cmd == ":help":
        _console.print(_SLASH_HELP)
        return None

    if cmd == ":sessions":
        from coda.cli.resume import render_sessions_table  # noqa: PLC0415

        rows = components.store.list_recent(limit=10)
        if not rows:
            _console.print("[dim]No sessions found in this workspace.[/dim]")
        else:
            _console.print(render_sessions_table(rows))
        return None

    if cmd == ":undo":
        import typer  # noqa: PLC0415

        from coda.cli.undo import undo_command  # noqa: PLC0415

        try:
            undo_command(
                session=components.session_id,
                _workspace_root=components.workspace.root,
            )
        except typer.Exit:
            # undo prints its own error/success message; the REPL survives.
            pass
        return None

    if cmd == ":cost":
        from coda.obs.cost import format_token_count  # noqa: PLC0415

        tracker = components.cost_tracker
        if tracker.total_tokens == 0:
            _console.print("[dim]No token usage recorded in this session yet.[/dim]")
            return None
        line = (
            f"tokens: {format_token_count(tracker.prompt_tokens)} in / "
            f"{format_token_count(tracker.completion_tokens)} out"
        )
        if tracker.cost_usd is not None:
            line += f" | cost ${tracker.cost_usd:.4f}"
        _console.print(line)
        return None

    if cmd == ":resume":
        if not args:
            _console.print("[yellow]Usage: :resume <session-id>[/yellow]")
            return None

        import typer  # noqa: PLC0415

        from coda.cli.resume import _resolve_session_id  # noqa: PLC0415

        try:
            resolved = _resolve_session_id(components.store, args[0])  # type: ignore[arg-type]
        except typer.Exit:
            # Ambiguous prefix — the resolver already printed the matches.
            return None
        if resolved is None:
            _console.print(f"[red]No session matching '{args[0]}' in this workspace.[/red]")
            return None
        if resolved == components.session_id:
            _console.print("[dim]Already in this session.[/dim]")
            return None
        return resolved

    _console.print(f"[yellow]Unknown command '{cmd}'. Type :help for available commands.[/yellow]")
    return None
