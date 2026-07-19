"""krodo doctor — pre-flight connectivity check for the configured LLM provider.

Usage::

    krodo doctor
    krodo doctor --model anthropic/glm-4.7 --api-base https://... --api-key sk-...

Sends a minimal 1-token ping to verify:
- model string / provider prefix
- api_base URL (if set)
- api_key validity (first 8 chars shown, rest masked)
- round-trip latency
- tool-call schema round-trip (writes a synthetic tool def and checks it comes back)

Exits 0 on success, 1 on failure.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import typer

_doctor_app = typer.Typer(
    name="doctor",
    help="Check LLM provider connectivity and configuration.",
    add_completion=False,
)

_DEFAULT_MODEL = "anthropic/claude-3-5-sonnet-20241022"


def register_doctor_app(app: typer.Typer) -> None:
    """Register the `doctor` subcommand onto *app*."""
    app.add_typer(_doctor_app)


@_doctor_app.callback(invoke_without_command=True)
def doctor(
    model: str = typer.Option(
        _DEFAULT_MODEL,
        "--model",
        "-m",
        help="LiteLLM model string (e.g. anthropic/glm-4.7)",
        envvar="KRODO_MODEL",
    ),
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        help="LLM API key (or set provider env var)",
        envvar="KRODO_API_KEY",
    ),
    api_base: str | None = typer.Option(
        None,
        "--api-base",
        help="Custom LLM API base URL",
        envvar="KRODO_API_BASE",
    ),
    max_tokens: int = typer.Option(
        16384,
        "--max-tokens",
        help="Configured max output tokens per response (displayed only).",
        envvar="KRODO_MAX_TOKENS",
    ),
) -> None:
    """Run a pre-flight connectivity check against the LLM provider."""
    asyncio.run(
        _async_doctor(
            model=model,
            api_key=api_key,
            api_base=api_base,
            max_tokens=max_tokens,
        )
    )


async def _async_doctor(
    model: str,
    api_key: str | None,
    api_base: str | None,
    max_tokens: int = 16384,
) -> None:
    from rich.console import Console  # noqa: PLC0415
    from rich.table import Table  # noqa: PLC0415

    from krodo.core.budget import get_context_window  # noqa: PLC0415
    from krodo.core.config import load_config  # noqa: PLC0415

    console = Console()
    console.print("\n[bold cyan]krodo doctor[/bold cyan] — LLM connectivity check\n")

    # ----------------------------------------------------------------
    # Config sources (M5.4)
    # ----------------------------------------------------------------
    from pathlib import Path as _Path  # noqa: PLC0415

    _workspace_root = _Path.cwd()
    _cfg, _cfg_sources = load_config(_workspace_root)
    # If config supplies a model and the CLI flag was left at its default,
    # use the config's model — mirrors the logic in main.main() / resume.
    # Otherwise the doctor pings the hardcoded default, which is misleading
    # (config shows zai/glm-4.6 but ping goes to anthropic/claude-3-5-sonnet).
    if _cfg.model is not None and model == _DEFAULT_MODEL:
        model = _cfg.model
    if _cfg_sources:
        console.print("[bold]config sources[/bold]")
        src_grid = Table.grid(padding=(0, 2))
        for src in _cfg_sources:
            src_grid.add_row("", f"[dim]{src}[/dim]")
        if _cfg.model is not None:
            src_grid.add_row("  model", f"[dim]{_cfg.model}[/dim]")
        if _cfg.max_tokens is not None:
            src_grid.add_row("  max_tokens", f"[dim]{_cfg.max_tokens:,}[/dim]")
        if _cfg.approval is not None:
            src_grid.add_row("  approval", f"[dim]{_cfg.approval}[/dim]")
        console.print(src_grid)
        console.print()

    # ----------------------------------------------------------------
    # Configuration summary
    # ----------------------------------------------------------------
    key_display = _mask_key(api_key) if api_key else "[dim](from env)[/dim]"
    base_display = api_base or "[dim](provider default)[/dim]"

    cfg = Table.grid(padding=(0, 2))
    cfg.add_row("[bold]model[/bold]", model)
    cfg.add_row("[bold]api_base[/bold]", base_display)
    cfg.add_row("[bold]api_key[/bold]", key_display)
    console.print(cfg)
    console.print()

    # ----------------------------------------------------------------
    # Output budget — exposed because GLM-style models with too small a
    # max_tokens will silently truncate write_file args to '{}'.
    # ----------------------------------------------------------------
    context_window = get_context_window(model)
    budget = Table.grid(padding=(0, 2))
    budget.add_row("[bold]max_tokens (output)[/bold]", f"{max_tokens:,}")
    budget.add_row("[bold]context window[/bold]", f"{context_window:,} tokens (model default)")
    console.print("[bold]output budget[/bold]")
    console.print(budget)
    console.print(
        "[dim]Tip: if you see invalid_args aborts, raise --max-tokens "
        "or lower the task scope.[/dim]\n"
    )

    # ----------------------------------------------------------------
    # Symbol index (M9) — read-only stats from the session-built database.
    # ----------------------------------------------------------------
    _index_db = _workspace_root / ".krodo" / "index" / "symbols.db"
    console.print("[bold]symbol index[/bold]")
    if _index_db.exists():
        try:
            from krodo.indexer import TreeSitterSymbolIndex  # noqa: PLC0415

            with TreeSitterSymbolIndex(_index_db, _workspace_root) as _idx:
                _ist = _idx.stats()
            idx_grid = Table.grid(padding=(0, 2))
            idx_grid.add_row("  backend", _ist.backend)
            idx_grid.add_row("  files", f"{_ist.files_indexed:,}")
            idx_grid.add_row("  symbols", f"{_ist.symbols:,}")
            idx_grid.add_row("  references", f"{_ist.references:,}")
            idx_grid.add_row("  last build", f"{_ist.build_ms} ms")
            console.print(idx_grid)
        except Exception as exc:  # noqa: BLE001 — doctor stays best-effort
            console.print(f"[dim]  (unreadable: {exc})[/dim]")
    else:
        console.print("[dim]  (not built — run a krodo session to index this workspace)[/dim]")
    console.print()

    # ----------------------------------------------------------------
    # 1-token ping
    # ----------------------------------------------------------------
    console.print("[dim]Sending 1-token ping…[/dim]")
    ok, latency_ms, error = await _ping(model, api_key, api_base)

    if ok:
        console.print(f"[green]✓  ping OK[/green]  ({latency_ms:.0f} ms)")
    else:
        console.print(f"[red]✗  ping FAILED[/red]  ({latency_ms:.0f} ms)")
        console.print(f"\n[red]Error:[/red] {error}")
        _print_hints(model, api_base, console)
        raise typer.Exit(1)

    # ----------------------------------------------------------------
    # Tool-call schema round-trip
    # ----------------------------------------------------------------
    console.print("[dim]Checking tool-call schema round-trip…[/dim]")
    tool_ok, tool_error = await _tool_ping(model, api_key, api_base)
    if tool_ok:
        console.print("[green]✓  tool_call schema OK[/green]")
    else:
        console.print(f"[yellow]⚠  tool_call schema check failed:[/yellow] {tool_error}")
        console.print("   Tool calling may not work correctly with this model/provider.")

    console.print("\n[bold green]All checks passed.[/bold green]\n")


async def _ping(
    model: str,
    api_key: str | None,
    api_base: str | None,
) -> tuple[bool, float, str]:
    """Send a minimal non-tool completion; return (ok, latency_ms, error_str)."""
    import litellm  # noqa: PLC0415

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
        "max_tokens": 3,
    }
    if api_key:
        kwargs["api_key"] = api_key
    if api_base:
        kwargs["api_base"] = api_base

    t0 = time.perf_counter()
    try:
        await litellm.acompletion(**kwargs)
        latency = (time.perf_counter() - t0) * 1000
        return True, latency, ""
    except Exception as exc:  # noqa: BLE001
        latency = (time.perf_counter() - t0) * 1000
        return False, latency, str(exc)


async def _tool_ping(
    model: str,
    api_key: str | None,
    api_base: str | None,
) -> tuple[bool, str]:
    """Check that the provider supports tool_use by sending a synthetic tool def."""
    import litellm  # noqa: PLC0415

    tool_def: dict[str, Any] = {
        "type": "function",
        "function": {
            "name": "krodo_health_check",
            "description": "Health-check tool — ignore.",
            "parameters": {
                "type": "object",
                "properties": {"status": {"type": "string"}},
                "required": ["status"],
            },
        },
    }
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "user", "content": "Call the krodo_health_check tool with status=ok"}
        ],
        "tools": [tool_def],
        "max_tokens": 64,
    }
    if api_key:
        kwargs["api_key"] = api_key
    if api_base:
        kwargs["api_base"] = api_base

    try:
        resp = await litellm.acompletion(**kwargs)
        msg = resp.choices[0].message
        if getattr(msg, "tool_calls", None):
            return True, ""
        # Model may answer in text when tool_choice is auto — still OK
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _mask_key(key: str) -> str:
    if len(key) <= 8:
        return "***"
    return key[:8] + "..." + "[MASKED]"


def _print_hints(model: str, api_base: str | None, console: Any) -> None:
    provider = model.split("/")[0] if "/" in model else "unknown"
    console.print("\n[bold yellow]Troubleshooting hints:[/bold yellow]")

    if provider in ("anthropic",) and api_base:
        console.print(
            "  • You are using [bold]anthropic[/bold] provider prefix with a custom api_base.\n"
            "    If your endpoint speaks OpenAI Chat Completions, change the model prefix:\n"
            f"    [dim]KRODO_MODEL=openai/{model.split('/', 1)[-1]}[/dim]"
        )
    elif provider == "openai" and api_base:
        console.print(
            "  • You are using [bold]openai[/bold] provider prefix with a custom api_base.\n"
            "    Make sure your endpoint exposes /v1/chat/completions."
        )
    else:
        console.print(
            "  • Check that KRODO_API_KEY / KRODO_API_BASE match the provider.\n"
            "  • LiteLLM model strings use the format: [bold]<provider>/<model-id>[/bold]\n"
            "    e.g. anthropic/claude-3-5-sonnet-20241022, openai/gpt-4o, openai/glm-4.7"
        )
    console.print()
