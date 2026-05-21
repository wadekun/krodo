"""Coda interactive REPL — M4.9.

The simplest possible multi-turn loop: read a line from stdin, hand it to
the shared `AgentLoop`, print the result, repeat.  Conversation history
survives across turns because the *same* `AgentLoop.context_manager` is
reused (see [coda.core.loop.AgentLoop] docstring).

This is the `input()`-based REPL called out by M1 plan §1; the
`prompt_toolkit` upgrade (arrow-key history, multi-line input, slash
commands) is deliberately deferred to M6.

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
from typing import TYPE_CHECKING

from rich.console import Console

if TYPE_CHECKING:
    from coda.cli.main import SessionComponents

_EXIT_TOKENS = {"exit", "quit", ":q", "\\q"}

_console = Console()


async def run_repl(components: SessionComponents) -> None:
    """Drive the interactive multi-turn loop until the user exits.

    `components` is shared across every turn so that:
      - `AgentLoop.context_manager` accumulates conversation history,
      - the `SessionEventLogger` keeps appending to one JSONL file,
      - the `GitCheckpointManager` accumulates checkpoints,
      - `session_id` stays stable for the whole REPL lifetime.
    """
    _console.print(
        "[dim]REPL mode. Type a prompt, 'exit' / Ctrl-D to quit. "
        "Press Ctrl-C twice to force exit.[/dim]\n"
    )

    turn_idx = 0
    last_ctrl_c = False

    while True:
        # ----------------------------------------------------------------
        # Read one line from the user.
        # ----------------------------------------------------------------
        try:
            # `input()` blocks the event loop, so run it in a worker thread.
            # This keeps asyncio happy and (more importantly) lets a Ctrl-C
            # delivered during a long turn propagate cleanly via cancellation.
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
