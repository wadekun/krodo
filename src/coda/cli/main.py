"""Coda CLI entry point.

Usage::

    coda [OPTIONS] PROMPT
    coda --root /path/to/project "add docstrings to src/main.py"

Environment variables:
    CODA_ROOT         override workspace root (lowest priority after --root)
    CODA_MODEL        LiteLLM model string, e.g. anthropic/claude-3-5-sonnet
    CODA_API_KEY      forwarded to LiteLLM as api_key
    CODA_API_BASE     forwarded to LiteLLM as api_base (custom endpoint)
    CODA_APPROVAL     approval mode: read_only | auto_edit | full_auto
"""

from __future__ import annotations

import uuid
from pathlib import Path

import typer

from coda.cli.banner import print_banner
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
        )
    )


async def _async_main(
    prompt: str,
    root: Path | None,
    model: str,
    api_key: str | None,
    api_base: str | None,
    approval_mode: str,
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

    logger.info(
        "session_init session_id=%s model=%s approval=%s",
        session_id,
        model,
        approval_mode,
    )

    # 5. Wire up dependencies
    sandbox = LocalSandboxRunner(workspace)
    approval_manager = TerminalApprovalManager(mode=approval_mode)  # type: ignore[arg-type]

    provider = LiteLLMProvider(
        model=model,
        api_key=api_key,
        api_base=api_base,
    )

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

    loop = AgentLoop(
        provider=provider,
        registry=registry,
        tool_ctx=tool_ctx,
        approval=approval_manager,
        config=LoopConfig(),
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
