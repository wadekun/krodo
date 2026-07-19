"""Krodo CLI entry point.

Usage::

    krodo                            # interactive REPL (M4.9)
    krodo [OPTIONS] PROMPT           # one-shot headless mode
    krodo --root /path/to/project "add docstrings to src/main.py"

Environment variables:
    KRODO_ROOT           override workspace root (lowest priority after --root)
    KRODO_MODEL          LiteLLM model string, e.g. anthropic/claude-3-5-sonnet
    KRODO_API_KEY        forwarded to LiteLLM as api_key
    KRODO_API_BASE       forwarded to LiteLLM as api_base (custom endpoint)
    KRODO_APPROVAL       approval mode: read_only | auto_edit | full_auto
    KRODO_COMPRESS       compression strategy: llm (default) | algorithmic
    KRODO_TOKEN_RATIO    token ratio multiplier for non-GPT models (default 1.0,
                        Claude 1.1×)
    KRODO_MAX_TOKENS     max output tokens per LLM response (default 16384)
"""

from __future__ import annotations

import json as _json
import uuid
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from rich.console import Console

from krodo.cli.banner import print_banner
from krodo.cli.doctor import register_doctor_app
from krodo.cli.group import KrodoGroup
from krodo.cli.resume import register_resume_app
from krodo.cli.undo import register_undo_app
from krodo.core.budget import BudgetCalculator, get_context_window
from krodo.core.compression import make_compressor
from krodo.core.config import load_config, resolve_symbol_backend
from krodo.core.context import InMemoryContextManager
from krodo.core.events import SessionEventLogger
from krodo.core.loop import AgentLoop, LoopConfig
from krodo.core.workspace import LocalWorkspaceResolver, Workspace
from krodo.llm.litellm_provider import LiteLLMProvider
from krodo.memory.agents_md import load_agents_md
from krodo.memory.store import JsonlSessionStore, SessionStore
from krodo.obs.cost import CostTracker, format_token_count
from krodo.obs.logger import configure_logging, get_session_log_path
from krodo.sandbox.approval import TerminalApprovalManager
from krodo.sandbox.checkpoint import GitCheckpointManager
from krodo.sandbox.firewall import LocalSandboxRunner
from krodo.sandbox.ignore import KrodoIgnore
from krodo.tools.builtin.fs import EditFileTool, ReadFileTool, WriteFileTool
from krodo.tools.builtin.git import GitCommitTool, GitDiffTool, GitStatusTool
from krodo.tools.builtin.patch import ApplyPatchTool
from krodo.tools.builtin.search import GlobTool, GrepTool, ListDirTool
from krodo.tools.builtin.shell import RunShellTool
from krodo.tools.protocols import ToolContext
from krodo.tools.registry import ToolRegistry

if TYPE_CHECKING:
    import logging

    from krodo.core.loop import TurnResult
    from krodo.indexer.base import SymbolBackend

# Module-level Rich Console for shared rendering (banners, panels, the
# prompt→response "Thinking…" spinner). Same instance as banner.py / repl.py
# pattern; using one console avoids the spinner and streamed text fighting
# over different output streams.
_console = Console(stderr=False)


def _version_callback(value: bool) -> None:
    """`--version` / `-V` eager callback: print version and exit immediately.

    Eager means Typer evaluates this before any other option or subcommand
    dispatch, so `krodo --version` works even with extra args on the line.
    """
    if value:
        from krodo import __version__  # noqa: PLC0415

        typer.echo(f"krodo {__version__}")
        raise typer.Exit()


app = typer.Typer(
    name="krodo",
    cls=KrodoGroup,
    help="Krodo — local-first AI coding agent.",
    add_completion=False,
    no_args_is_help=False,
    invoke_without_command=True,
)

# Register sub-commands (add_typer does not change the main callback behaviour)
register_undo_app(app)
register_resume_app(app)
register_doctor_app(app)

_DEFAULT_MODEL = "anthropic/claude-3-5-sonnet-20241022"

# LiteLLM issue #14011: Anthropic server-side web_search returns `server_tool_use`
# content blocks that LiteLLM's Pydantic response models don't yet fully
# recognise. Pydantic emits a UserWarning ("PydanticSerializationUnexpectedValue:
# Expected `ServerToolUse` ...") when re-serialising these blocks. The warning is
# non-fatal — serialization still completes, krodo sees the tool_use content
# correctly — but it spams stderr on every Claude web-search call. Filter this
# specific message until LiteLLM ships the fix upstream. Remove the filter once
# we upgrade to a LiteLLM version that expands the Anthropic response union.
warnings.filterwarnings(
    "ignore",
    message=r".*PydanticSerializationUnexpectedValue.*ServerToolUse.*",
    category=UserWarning,
)


# ---------------------------------------------------------------------------
# Module-level helpers (shared between headless and REPL modes)
# ---------------------------------------------------------------------------


# User-facing one-liner explanations of every AbortReason emitted by AgentLoop.
# Shared so both headless and REPL render identical messages.
ABORT_REASONS: dict[str, str] = {
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


@dataclass
class SessionComponents:
    """Bundle of long-lived objects shared across one Krodo session.

    Reused by both headless single-shot runs and the REPL multi-turn loop
    so that the AgentLoop's context_manager (and therefore conversation
    history) survives across turns.
    """

    workspace: Workspace
    loop: AgentLoop
    logger: logging.Logger
    session_id: str
    event_logger: SessionEventLogger
    store: SessionStore
    sessions_path: Path  # <workspace>/.krodo/sessions/<id>.jsonl — event log
    log_path: Path  # <workspace>/.krodo/logs/<id>.log — structlog application log
    max_tokens: int
    cost_tracker: CostTracker
    approval: TerminalApprovalManager
    # M9: symbol index (None when symbol_backend == "off"). Surfaced so the
    # REPL / doctor can read stats without re-opening the database.
    indexer: SymbolBackend | None = None


def _collect_written_paths(sessions_path: Path) -> list[str]:
    """Return deduplicated file paths from CHECKPOINT events in the session JSONL.

    Reads the session event file (``<workspace>/.krodo/sessions/<id>.jsonl``);
    returns an empty list if the file is missing or unparseable.
    """
    paths: list[str] = []
    seen: set[str] = set()
    try:
        if not sessions_path.exists():
            return paths
        for raw in sessions_path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                obj = _json.loads(stripped)
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


def _cost_summary_line(components: SessionComponents) -> str | None:
    """Render 'tokens     : 12.3k in / 4.1k out | cost $0.0231' (None if no usage)."""
    tracker = components.cost_tracker
    if tracker.total_tokens == 0:
        return None
    line = (
        f"tokens     : {format_token_count(tracker.prompt_tokens)} in / "
        f"{format_token_count(tracker.completion_tokens)} out"
    )
    if tracker.cost_usd is not None:
        line += f" | cost ${tracker.cost_usd:.4f}"
    return line


def print_session_summary(components: SessionComponents, turns: int | None = None) -> None:
    """Print the standard '─── session summary ───' block to stderr.

    Shared between headless mode (called once after the single run) and REPL
    mode (called once when the user exits). Pass `turns` to also display the
    cumulative turn count (REPL only); headless mode omits it.
    """
    written_paths = _collect_written_paths(components.sessions_path)
    typer.echo("", err=True)
    typer.echo("─── session summary ───────────────────", err=True)
    typer.echo(f"workspace  : {components.workspace.root}", err=True)
    if turns is not None:
        typer.echo(f"turns      : {turns}", err=True)
    cost_line = _cost_summary_line(components)
    if cost_line:
        typer.echo(cost_line, err=True)
    # tool_calls cannot be aggregated from TurnResult here (REPL has many turns);
    # the session JSONL is the source of truth for per-session totals.
    if written_paths:
        typer.echo("files written:", err=True)
        for p in written_paths:
            typer.echo(f"  {p}", err=True)
    typer.echo(f"session    : {components.sessions_path}", err=True)
    typer.echo(f"log        : {components.log_path}", err=True)


# ---------------------------------------------------------------------------
# CLI callback
# ---------------------------------------------------------------------------


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    prompt: str | None = typer.Argument(None, help="Task to perform (omit to enter REPL)"),
    version: bool = typer.Option(  # noqa: ARG001 — handled by callback
        None,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
    root: Path | None = typer.Option(
        None,
        "--root",
        "-r",
        help="Workspace root (default: auto-discover from cwd)",
        envvar="KRODO_ROOT",
    ),
    model: str = typer.Option(
        _DEFAULT_MODEL,
        "--model",
        "-m",
        help="LiteLLM model string",
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
        envvar="KRODO_APPROVAL",
    ),
    max_tool_calls: int = typer.Option(
        25,
        "--max-tool-calls",
        help="Maximum tool calls per turn before the loop aborts.",
    ),
    max_tokens: int = typer.Option(
        16384,
        "--max-tokens",
        help=(
            "Maximum output tokens per LLM response. Raise this if responses "
            "get truncated mid-tool-call (default 16384). Forwarded to the "
            "LLM provider."
        ),
        envvar="KRODO_MAX_TOKENS",
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
    """Run Krodo with the given PROMPT, or enter REPL mode if no PROMPT is given."""
    # If a subcommand was invoked (e.g. krodo undo), skip the main loop
    if ctx.invoked_subcommand is not None:
        return

    # M5.4: Load config file defaults, applying them where CLI/env left defaults
    resolved_root = root.expanduser().resolve() if root is not None else None
    # We need workspace root to load config; use a quick resolver here
    _quick_ws_root = resolved_root or _quick_resolve_root()
    cfg, _cfg_sources = load_config(_quick_ws_root)

    from click.core import ParameterSource  # noqa: PLC0415

    # Apply config values where CLI flag is still at built-in default
    if cfg.model is not None and ctx.get_parameter_source("model") == ParameterSource.DEFAULT:
        model = cfg.model
    if cfg.api_base is not None and ctx.get_parameter_source("api_base") == ParameterSource.DEFAULT:
        api_base = cfg.api_base
    if cfg.approval is not None and ctx.get_parameter_source("approval") == ParameterSource.DEFAULT:
        approval = cfg.approval
    if (
        cfg.max_tokens is not None
        and ctx.get_parameter_source("max_tokens") == ParameterSource.DEFAULT
    ):
        max_tokens = cfg.max_tokens
    if (
        cfg.max_tool_calls is not None
        and ctx.get_parameter_source("max_tool_calls") == ParameterSource.DEFAULT
    ):
        max_tool_calls = cfg.max_tool_calls
    if (
        cfg.summary_window is not None
        and ctx.get_parameter_source("summary_window") == ParameterSource.DEFAULT
    ):
        summary_window = cfg.summary_window
    # prompt_cache has no CLI flag (rarely needs overriding); config.yaml
    # can opt out by setting `prompt_cache: false`. Defaults to True so
    # Anthropic system-prompt caching is on out of the box.
    prompt_cache_value = cfg.prompt_cache if cfg.prompt_cache is not None else True
    # M9: symbol index backend — config-only (no CLI flag). Defaults to
    # "treesitter" (on); set `symbol_backend: off` to skip building/injecting.
    symbol_backend_value = resolve_symbol_backend(cfg.symbol_backend)

    import asyncio  # noqa: PLC0415

    # M6.3: pipe entry — `echo task | krodo` runs headless; `git diff | krodo
    # "review"` appends piped stdin as a <stdin> context block.  Empty piped
    # stdin (e.g. CliRunner test streams) keeps the REPL behaviour unchanged.
    effective_prompt = prompt
    piped = _read_piped_stdin()
    if piped:
        if prompt:
            effective_prompt = f"{prompt}\n\n<stdin>\n{piped}\n</stdin>"
        else:
            effective_prompt = piped

    async def _entry() -> None:
        components = _build_session_components(
            root=root,
            model=model,
            api_key=api_key,
            api_base=api_base,
            approval_mode=approval,
            max_tool_calls=max_tool_calls,
            max_tokens=max_tokens,
            summary_window=summary_window,
            prompt_cache=prompt_cache_value,
            symbol_backend=symbol_backend_value,
        )
        if effective_prompt:
            await _run_headless(effective_prompt, components)
        else:
            # Import lazily to avoid cycles and keep startup fast.
            from krodo.cli.resume import repl_session_cycle  # noqa: PLC0415

            def _rebuild(target_id: str) -> SessionComponents:
                return _build_session_components(
                    root=root,
                    model=model,
                    api_key=api_key,
                    api_base=api_base,
                    approval_mode=approval,
                    max_tool_calls=max_tool_calls,
                    max_tokens=max_tokens,
                    summary_window=summary_window,
                    prompt_cache=prompt_cache_value,
                    symbol_backend=symbol_backend_value,
                    resume_session_id=target_id,
                )

            await repl_session_cycle(components, _rebuild)

    asyncio.run(_entry())


def _read_piped_stdin() -> str:
    """Return stripped piped stdin content, or '' when stdin is a TTY/empty."""
    import sys  # noqa: PLC0415

    try:
        if sys.stdin is None or sys.stdin.isatty():
            return ""
        return sys.stdin.read().strip()
    except (OSError, ValueError):
        return ""


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _quick_resolve_root() -> Path:
    """Best-effort workspace root for config loading (before full resolver runs).

    Uses the same priority chain as LocalWorkspaceResolver but avoids
    constructing a full Workspace (which validates writability, etc.).
    """
    import os as _os  # noqa: PLC0415

    env = _os.environ.get("KRODO_ROOT")
    if env:
        return Path(env).expanduser().resolve()

    cwd = Path.cwd().resolve()
    for parent in [cwd, *cwd.parents]:
        if (parent / ".krodo").exists() or (parent / ".git").exists():
            return parent
    return cwd


# ---------------------------------------------------------------------------
# Session bootstrap (formerly inlined inside _async_main)
# ---------------------------------------------------------------------------


def _build_symbol_index(
    workspace: Workspace,
    ignore: KrodoIgnore,
    symbol_backend: str,
    event_logger: SessionEventLogger,
    logger: logging.Logger,
) -> SymbolBackend | None:
    """Construct + build the symbol index, or return ``None`` when disabled.

    Emits an ``INDEX_BUILD`` event and prints a one-line status so the user can
    see the backend, symbol count, and build time. A build failure is logged
    and downgraded to "no index" — the session continues either way, since the
    index is an enhancement, not a hard dependency.
    """
    from rich.console import Console as _Console  # noqa: PLC0415

    if symbol_backend == "off":
        _Console(stderr=True).print("[dim]symbols: off[/dim]")
        return None

    # Canary: probe a sample of real workspace files in a subprocess before
    # touching tree-sitter in-process. A native crash (SIGSEGV/SIGBUS — see
    # docs/benchmarks/m9_symbol_index_perf_results.md) would otherwise kill
    # the whole session during build_full(); the canary lets us degrade to
    # "no index" instead. Best-effort only — see canary.py module docstring
    # for residual risk.
    from krodo.indexer import canary  # noqa: PLC0415

    canary_ok, canary_detail = canary.probe(workspace.root, ignore)
    if not canary_ok:
        logger.warning("symbol index canary probe failed: %s", canary_detail)
        _Console(stderr=True).print(
            "[yellow]symbols: canary probe failed (index disabled this session)[/yellow]"
        )
        return None

    from krodo.core.types import SessionEventType  # noqa: PLC0415
    from krodo.indexer import TreeSitterSymbolIndex  # noqa: PLC0415

    db_path = workspace.root / ".krodo" / "index" / "symbols.db"
    idx = TreeSitterSymbolIndex(db_path, workspace.root, ignore=ignore)
    try:
        stats = idx.build_full()
    except Exception:  # noqa: BLE001 — never abort the session over the index
        idx.close()
        logger.warning("symbol index build failed", exc_info=True)
        _Console(stderr=True).print("[yellow]symbols: build failed (index disabled)[/yellow]")
        return None

    event_logger.emit(
        SessionEventType.INDEX_BUILD,
        data={
            "backend": stats.backend,
            "files": stats.files_indexed,
            "symbols": stats.symbols,
            "references": stats.references,
            "build_ms": stats.build_ms,
        },
    )
    _Console(stderr=True).print(
        f"[dim]symbols: {stats.backend} | {stats.files_indexed} files, "
        f"{stats.symbols} symbols ({stats.build_ms} ms)[/dim]"
    )
    return idx


def _build_session_components(
    *,
    root: Path | None,
    model: str,
    api_key: str | None,
    api_base: str | None,
    approval_mode: str,
    max_tool_calls: int = 25,
    max_tokens: int = 16384,
    summary_window: int = 2,  # noqa: ARG001  (reserved for M5 compactor wiring)
    resume_session_id: str | None = None,
    prompt_cache: bool = True,
    symbol_backend: str = "treesitter",
) -> SessionComponents:
    """Wire workspace, logger, banner, provider, tools, and AgentLoop.

    Returns a `SessionComponents` bundle ready to be driven by either
    `_run_headless` (single turn) or `run_repl` (multi-turn).
    Banner is printed exactly once here — REPL mode reuses these
    components, so the banner naturally appears only on session start.

    Parameters
    ----------
    resume_session_id:
        When set, reuse the existing session with this ID instead of
        generating a new one.  ``store.create_session`` is NOT called
        (the session already exists); the event logger bootstraps its
        ``_seq`` from ``store.max_seq(session_id) + 1``.
    """
    session_id = resume_session_id or str(uuid.uuid4())[:8]

    # 1. Resolve workspace
    resolver = LocalWorkspaceResolver()
    workspace = resolver.resolve(explicit=root)

    # 2. Observability — configure logging first so all subsequent steps are traced
    logger = configure_logging(workspace, session_id)

    # 3. Print banner (invariant: visible before any tool execution)
    print_banner(workspace, approval_mode=approval_mode, model=model)

    # 4. full_auto warning banner
    if approval_mode == "full_auto":
        from rich.console import Console  # noqa: PLC0415
        from rich.panel import Panel  # noqa: PLC0415

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
    import os  # noqa: PLC0415

    compress_strategy = os.environ.get("KRODO_COMPRESS", "llm")
    context_window = get_context_window(model)
    from rich.console import Console as _Console  # noqa: PLC0415

    _Console(stderr=True).print(
        f"[dim]Model context window: {context_window:,} tokens | "
        f"Compression: {compress_strategy} | "
        f"Max tool calls: {max_tool_calls} | "
        f"Max output: {max_tokens:,} tokens[/dim]"
    )

    logger.info(
        "session_init session_id=%s model=%s approval=%s compress=%s window=%d max_tokens=%d",
        session_id,
        model,
        approval_mode,
        compress_strategy,
        context_window,
        max_tokens,
    )

    # 5. Wire up dependencies
    sandbox = LocalSandboxRunner(workspace)
    approval_manager = TerminalApprovalManager(mode=approval_mode)  # type: ignore[arg-type]

    provider = LiteLLMProvider(
        model=model,
        api_key=api_key,
        api_base=api_base,
        extra_kwargs={"max_tokens": max_tokens},
        # prompt_cache defaults to True; config.yaml can opt out (e.g. very
        # short sessions where cache-write cost outweighs the benefit).
        prompt_cache=prompt_cache,
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

    # M5.3: Pre-compute AGENTS.md bundle so the hash can go into SESSION_INIT
    bundle = load_agents_md(workspace, cwd=Path.cwd(), count_fn=provider.count_tokens)

    # M5.1: Session store + event logger
    store = JsonlSessionStore(workspace.root / ".krodo" / "sessions")
    if resume_session_id is None:
        # New session — write the SESSION_INIT header (seq=0)
        store.create_session(
            session_id,
            model=model,
            agents_md_hash=bundle.sha256(),
            initial_prompt_hash=None,
        )
    else:
        # Show resume banner now that store is available
        from rich.console import Console as _RichConsole  # noqa: PLC0415, N814

        prior_events = store.load_events(resume_session_id)
        user_turns = sum(1 for e in prior_events if e.type.value == "user_message")
        _RichConsole(stderr=True).print(
            f"[dim]Resuming session {resume_session_id} "
            f"({user_turns} prior turn(s), {len(prior_events)} events)[/dim]"
        )

    # When resuming, the session file already has events; from_store bootstraps
    # _seq from store.max_seq so cross-process appends never repeat a seq value.
    event_logger = SessionEventLogger.from_store(store, session_id, logger=logger)

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

    # M4: KrodoIgnore + GitCheckpointManager
    ignore = KrodoIgnore.from_workspace(workspace)
    checkpoint_mgr = GitCheckpointManager(workspace, logger=logger)

    # M9: symbol index (built once at session start; None when disabled).
    indexer = _build_symbol_index(workspace, ignore, symbol_backend, event_logger, logger)

    tool_ctx = ToolContext(
        workspace=workspace,
        sandbox=sandbox,
        session_id=session_id,
        logger=logger,
        ignore=ignore,
        checkpoint=checkpoint_mgr,
        event_logger=event_logger,
        indexer=indexer,
    )

    loop_config = LoopConfig(max_tool_calls_per_turn=max_tool_calls)
    cost_tracker = CostTracker()
    loop = AgentLoop(
        provider=provider,
        registry=registry,
        tool_ctx=tool_ctx,
        approval=approval_manager,
        config=loop_config,
        event_logger=event_logger,
        cost_tracker=cost_tracker,
    )

    # Inject budget + compressor into context manager.
    # Reuse the AgentLoop's already-rendered system prompt (which has the
    # current tool list substituted in) so we don't lose the dynamic
    # {tool_list} substitution by passing the raw template.
    loop.context_manager = InMemoryContextManager(
        system_prompt=loop.system_prompt,
        budget=budget,
        compressor=compressor,
    )

    # M5.3: Inject AGENTS.md bundle into the context manager history
    if bundle.content:
        from rich.console import Console as _RichConsole2  # noqa: N814, PLC0415

        from krodo.core.types import Message as _Msg  # noqa: PLC0415

        loop.context_manager._history.insert(  # noqa: SLF001
            0,
            _Msg(
                role="user",
                content=f"<project_memory>\n{bundle.content}\n</project_memory>",
            ),
        )
        source_names = ", ".join(p.name for p in bundle.sources)
        truncation_warn = " [yellow](truncated)[/yellow]" if bundle.truncated else ""
        _RichConsole2(stderr=True).print(
            f"[dim]memory: {len(bundle.sources)} file(s), "
            f"{bundle.total_tokens:,} tokens ({source_names}){truncation_warn}[/dim]"
        )

    sessions_path = workspace.root / ".krodo" / "sessions" / f"{session_id}.jsonl"
    log_path = Path(str(get_session_log_path(workspace, session_id)))

    return SessionComponents(
        workspace=workspace,
        loop=loop,
        logger=logger,
        session_id=session_id,
        event_logger=event_logger,
        store=store,
        sessions_path=sessions_path,
        log_path=log_path,
        max_tokens=max_tokens,
        cost_tracker=cost_tracker,
        approval=approval_manager,
        indexer=indexer,
    )


# ---------------------------------------------------------------------------
# Headless one-shot mode
# ---------------------------------------------------------------------------


async def _run_headless(prompt: str, components: SessionComponents) -> None:
    """Run a single AgentLoop turn and print the standard session summary.

    Used by `krodo PROMPT` invocations.  Behaviour is unchanged from the
    pre-M4.9 implementation: print result, then summary, then exit.
    """
    # "Thinking…" spinner: starts before the call, stops on first token via
    # on_first_token. finally() guarantees cleanup on error paths.
    status = _console.status("[dim]Thinking…[/dim]")
    status.start()
    try:
        result = await components.loop.run(prompt, on_first_token=status.stop)
    finally:
        # Idempotent: safe even if on_first_token already stopped it.
        status.stop()
    _echo_turn_result(result)
    _print_headless_summary(components, result)
    components.logger.info(
        "session_end tool_calls=%d aborted=%s hit_limit=%s",
        result.tool_calls_made,
        result.aborted_by_user,
        result.hit_tool_call_limit,
    )
    if components.indexer is not None:
        components.indexer.close()


def _echo_turn_result(result: TurnResult) -> None:
    """Print one turn's outcome (final text, abort reason, or limit warning).

    Shared between headless mode and REPL turns so users see identical
    diagnostics regardless of entry point.
    """
    if result.hit_tool_call_limit:
        typer.echo(
            f"⚠  Tool call limit reached ({result.tool_calls_made}). Task may be incomplete.",
            err=True,
        )
    elif result.aborted_by_user:
        reason_str = ABORT_REASONS.get(result.abort_reason, result.abort_reason)
        typer.echo(f"⚠  Task halted: {reason_str}", err=True)
    elif not result.streamed:
        # When streamed, the loop already rendered final_text live.
        typer.echo(result.final_text)


def _print_headless_summary(components: SessionComponents, result: TurnResult) -> None:
    """Headless-mode summary preserves the original layout (tool calls line)."""
    written_paths = _collect_written_paths(components.sessions_path)
    typer.echo("", err=True)
    typer.echo("─── session summary ───────────────────", err=True)
    typer.echo(f"workspace  : {components.workspace.root}", err=True)
    typer.echo(f"tool calls : {result.tool_calls_made}", err=True)
    cost_line = _cost_summary_line(components)
    if cost_line:
        typer.echo(cost_line, err=True)
    if written_paths:
        typer.echo("files written:", err=True)
        for p in written_paths:
            typer.echo(f"  {p}", err=True)
    typer.echo(f"session    : {components.sessions_path}", err=True)
    typer.echo(f"log        : {components.log_path}", err=True)
