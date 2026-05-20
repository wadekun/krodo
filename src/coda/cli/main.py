"""Coda CLI entry point.

Usage::

    coda [OPTIONS] PROMPT
    coda --root /path/to/project "add docstrings to src/main.py"

Environment variables:
    CODA_ROOT           override workspace root (lowest priority after --root)
    CODA_MODEL          LiteLLM model string, e.g. anthropic/claude-3-5-sonnet
    CODA_API_KEY        forwarded to LiteLLM as api_key
    CODA_API_BASE       forwarded to LiteLLM as api_base (custom endpoint)
    CODA_APPROVAL       approval mode: read_only | auto_edit | full_auto
    CODA_COMPRESS       compression strategy: llm (default) | algorithmic
    CODA_TOKEN_RATIO    token ratio multiplier for non-GPT models (default 1.0,
                        Claude 1.1×)
"""

from __future__ import annotations

import uuid
from pathlib import Path

import typer

from coda.cli.banner import print_banner
from coda.cli.doctor import register_doctor_app
from coda.cli.undo import register_undo_app
from coda.core.budget import BudgetCalculator, get_context_window
from coda.core.compression import make_compressor
from coda.core.context import InMemoryContextManager
from coda.core.events import SessionEventLogger
from coda.core.loop import AgentLoop, LoopConfig
from coda.core.workspace import LocalWorkspaceResolver
from coda.llm.litellm_provider import LiteLLMProvider
from coda.obs.logger import configure_logging, get_session_log_path
from coda.sandbox.approval import TerminalApprovalManager
from coda.sandbox.checkpoint import GitCheckpointManager
from coda.sandbox.firewall import LocalSandboxRunner
from coda.sandbox.ignore import CodaIgnore
from coda.tools.builtin.fs import EditFileTool, ReadFileTool, WriteFileTool
from coda.tools.builtin.git import GitCommitTool, GitDiffTool, GitStatusTool
from coda.tools.builtin.patch import ApplyPatchTool
from coda.tools.builtin.search import GlobTool, GrepTool, ListDirTool
from coda.tools.builtin.shell import RunShellTool
from coda.tools.protocols import ToolContext
from coda.tools.registry import ToolRegistry

app = typer.Typer(
    name="coda",
    help="Coda — local-first AI coding agent.",
    add_completion=False,
    no_args_is_help=False,
    invoke_without_command=True,
)

# Register sub-commands (add_typer does not change the main callback behaviour)
register_undo_app(app)
register_doctor_app(app)

_DEFAULT_MODEL = "anthropic/claude-3-5-sonnet-20241022"


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    prompt: str | None = typer.Argument(None, help="Task to perform"),
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
        help="LiteLLM model string",
        envvar="CODA_MODEL",
    ),
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        help="LLM API key (or set provider env var)",
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
        help=(
            "Approval mode: "
            "read_only (deny all writes), "
            "auto_edit (prompt for writes, default), "
            "full_auto (approve everything — use with caution)"
        ),
        envvar="CODA_APPROVAL",
    ),
    max_tool_calls: int = typer.Option(
        15,
        "--max-tool-calls",
        help="Maximum tool calls per turn before the loop aborts.",
    ),
    summary_window: int = typer.Option(
        2,
        "--summary-window",
        help=(
            "Number of dialogue rounds to compress in one pass "
            "(used by LLM and algorithmic compressors)."
        ),
    ),
) -> None:
    """Run Coda with the given PROMPT."""
    # If a subcommand was invoked (e.g. coda undo), skip the main loop
    if ctx.invoked_subcommand is not None:
        return

    if not prompt:
        typer.echo(ctx.get_help())
        raise typer.Exit()

    import asyncio

    asyncio.run(
        _async_main(
            prompt=prompt,
            root=root,
            model=model,
            api_key=api_key,
            api_base=api_base,
            approval_mode=approval,
            max_tool_calls=max_tool_calls,
            summary_window=summary_window,
        )
    )


def _collect_written_paths(log_path: object) -> list[str]:
    """Return deduplicated file paths from CHECKPOINT events in the session log.

    Reads the JSONL log after the session ends.  Returns an empty list if the
    log is missing or unparseable.
    """
    import json as _json  # noqa: PLC0415
    from pathlib import Path as _Path  # noqa: PLC0415

    paths: list[str] = []
    seen: set[str] = set()
    try:
        lp = _Path(str(log_path))
        if not lp.exists():
            return paths
        for raw in lp.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = _json.loads(raw)
            except _json.JSONDecodeError:
                continue
            if obj.get("type") == "checkpoint":
                for p in obj.get("data", {}).get("affected_paths", []):
                    if p not in seen:
                        seen.add(p)
                        paths.append(p)
    except OSError:
        pass
    return paths


async def _async_main(
    prompt: str,
    root: Path | None,
    model: str,
    api_key: str | None,
    api_base: str | None,
    approval_mode: str,
    max_tool_calls: int = 15,
    summary_window: int = 2,
) -> None:
    session_id = str(uuid.uuid4())[:8]

    # 1. Resolve workspace
    resolver = LocalWorkspaceResolver()
    workspace = resolver.resolve(explicit=root)

    # 2. Observability — configure logging first so all subsequent steps are traced
    logger = configure_logging(workspace, session_id)

    # 3. Print banner (invariant: visible before any tool execution)
    print_banner(workspace, approval_mode=approval_mode)

    # 4. full_auto warning banner
    if approval_mode == "full_auto":
        from rich.console import Console
        from rich.panel import Panel

        Console().print(
            Panel(
                "[bold red]WARNING: full_auto mode active.[/bold red]\n"
                "All tools will execute without any approval prompts.\n"
                "Ensure you trust the task and workspace before proceeding.",
                title="[red]⚠  Security Warning[/red]",
                border_style="red",
            )
        )

    # 4b. Compression strategy banner
    import os

    compress_strategy = os.environ.get("CODA_COMPRESS", "llm")
    context_window = get_context_window(model)
    from rich.console import Console as _Console

    _Console(stderr=True).print(
        f"[dim]Model context window: {context_window:,} tokens | "
        f"Compression: {compress_strategy} | "
        f"Max tool calls: {max_tool_calls}[/dim]"
    )

    logger.info(
        "session_init session_id=%s model=%s approval=%s compress=%s window=%d",
        session_id,
        model,
        approval_mode,
        compress_strategy,
        context_window,
    )

    # 5. Wire up dependencies
    sandbox = LocalSandboxRunner(workspace)
    approval_manager = TerminalApprovalManager(mode=approval_mode)  # type: ignore[arg-type]

    provider = LiteLLMProvider(
        model=model,
        api_key=api_key,
        api_base=api_base,
    )

    # M3: Budget calculator + compressor
    budget = BudgetCalculator(
        model=model,
        count_fn=provider.count_message_tokens,
    )
    try:
        compressor = make_compressor(strategy=compress_strategy, provider=provider)
    except ValueError:
        compressor = make_compressor(strategy="algorithmic")

    # M3: Session event logger
    event_logger = SessionEventLogger.from_workspace_path(session_id, workspace.root)

    registry = ToolRegistry()
    # M1 tools
    registry.register(ReadFileTool())
    registry.register(WriteFileTool())
    registry.register(RunShellTool())
    # M2 tools
    registry.register(EditFileTool())
    registry.register(ListDirTool())
    registry.register(GlobTool())
    registry.register(GrepTool())
    registry.register(ApplyPatchTool())
    registry.register(GitStatusTool())
    registry.register(GitDiffTool())
    registry.register(GitCommitTool())

    # M4: CodaIgnore + GitCheckpointManager
    ignore = CodaIgnore.from_workspace(workspace)
    checkpoint_mgr = GitCheckpointManager(workspace, logger=logger)

    tool_ctx = ToolContext(
        workspace=workspace,
        sandbox=sandbox,
        session_id=session_id,
        logger=logger,
        ignore=ignore,
        checkpoint=checkpoint_mgr,
        event_logger=event_logger,
    )

    loop_config = LoopConfig(max_tool_calls_per_turn=max_tool_calls)
    loop = AgentLoop(
        provider=provider,
        registry=registry,
        tool_ctx=tool_ctx,
        approval=approval_manager,
        config=loop_config,
        event_logger=event_logger,
    )

    # Inject budget + compressor into context manager
    loop.context_manager = InMemoryContextManager(
        system_prompt=loop_config.system_prompt,
        budget=budget,
        compressor=compressor,
    )

    # 6. Run
    result = await loop.run(prompt)

    _abort_reasons = {
        "denied": "you denied the approval prompt",
        "stall": "agent stalled (3× identical write call) — try rephrasing or use full_auto",
        "bad_json": "model returned invalid tool-call JSON after 2 retries",
        "provider": "LLM provider error after 3 retries — check API key / quota",
        "max_tokens": "model output was truncated (max_tokens) — task split hint was injected",
        "invalid_args": (
            "tool calls had invalid arguments after 3 attempts "
            "(likely LLM output truncation) — try a smaller task or a model with "
            "higher max_output_tokens"
        ),
    }
    log_path = get_session_log_path(workspace, session_id)
    written_paths = _collect_written_paths(log_path)

    if result.hit_tool_call_limit:
        typer.echo(
            f"⚠  Tool call limit reached ({result.tool_calls_made}). Task may be incomplete.",
            err=True,
        )
    elif result.aborted_by_user:
        reason_str = _abort_reasons.get(result.abort_reason, result.abort_reason)
        typer.echo(f"⚠  Task halted: {reason_str}", err=True)
    else:
        typer.echo(result.final_text)

    # Always print session summary so the user knows where files went and where to debug.
    typer.echo("", err=True)
    typer.echo("─── session summary ───────────────────", err=True)
    typer.echo(f"workspace  : {workspace.root}", err=True)
    typer.echo(f"tool calls : {result.tool_calls_made}", err=True)
    if written_paths:
        typer.echo("files written:", err=True)
        for p in written_paths:
            typer.echo(f"  {p}", err=True)
    typer.echo(f"session log: {log_path}", err=True)

    logger.info(
        "session_end tool_calls=%d aborted=%s hit_limit=%s",
        result.tool_calls_made,
        result.aborted_by_user,
        result.hit_tool_call_limit,
    )
