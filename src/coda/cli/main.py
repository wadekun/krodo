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
from coda.core.budget import BudgetCalculator, get_context_window
from coda.core.compression import make_compressor
from coda.core.context import InMemoryContextManager
from coda.core.events import SessionEventLogger
from coda.core.loop import AgentLoop, LoopConfig
from coda.core.workspace import LocalWorkspaceResolver
from coda.llm.litellm_provider import LiteLLMProvider
from coda.obs.logger import configure_logging
from coda.sandbox.approval import TerminalApprovalManager
from coda.sandbox.firewall import LocalSandboxRunner
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
    no_args_is_help=True,
)

_DEFAULT_MODEL = "anthropic/claude-3-5-sonnet-20241022"


@app.command()
def main(
    prompt: str = typer.Argument(..., help="Task to perform"),
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

    tool_ctx = ToolContext(
        workspace=workspace,
        sandbox=sandbox,
        session_id=session_id,
        logger=logger,
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

    if result.hit_tool_call_limit:
        typer.echo(
            f"⚠  Tool call limit reached ({result.tool_calls_made}). Task may be incomplete.",
            err=True,
        )
    elif result.aborted_by_user:
        typer.echo("⚠  Task aborted by user.", err=True)
    else:
        typer.echo(result.final_text)

    logger.info(
        "session_end tool_calls=%d aborted=%s hit_limit=%s",
        result.tool_calls_made,
        result.aborted_by_user,
        result.hit_tool_call_limit,
    )
