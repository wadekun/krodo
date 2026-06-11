# Project memory: Coda

> This file is auto-loaded into the system prompt of every Coda session running in this repository (see `docs/architecture.md` §5.6). Keep it concise — total budget is 8K tokens.

## What this project is

Coda is a local-first, multi-provider coding agent CLI. We are currently in **Phase 1 (MVP)**: building a usable REPL + headless `coda exec` with 11 tools, three approval modes, automatic git checkpoints, JSONL-backed sessions, and AGENTS.md project memory. We are explicitly **not** building TUI / RAG / Docker sandbox / IDE plugins / Web UI in this phase.

The single source of truth for design decisions is [`docs/architecture.md`](docs/architecture.md). When in doubt, read it first; if the design is unclear or wrong, update the doc in the same PR as the code.

## Engineering rules (the seven principles, see architecture.md §11)

1. Make it work first; optimize later.
2. **Protocol first** — every core module begins as a `typing.Protocol` in `src/coda/<module>/base.py`.
3. **Safe by default** — deny unless explicitly allowed; dangerous ops always prompt; sensitive files always ignored.
4. **Day 1 observability** — every tool call gets a trace span; every LLM call records tokens + cost. PRs without trace/log are not merged.
5. **Git is the safety net** — checkpoint with `git stash create` before every write; `coda undo` restores.
6. Extract shared abstractions only after a pattern repeats across **three** modules.
7. **Pin major versions** of LLM/agent core dependencies (`litellm>=1.40,<2`, `anthropic` SDK, etc.). Minor upgrades require a PR review and a passing regression run.

## Code conventions

- Python **3.12+ only**. Use modern syntax: `list[int]`, `int | None`, `match` statements, `@override`.
- Strict typing: `mypy --strict` must pass with zero errors. No `# type: ignore` without an inline reason.
- Lint: `ruff check` with rules `E F I N W UP B S ASYNC T20`. **Never use `print()` in `src/`** — use `structlog`.
- `subprocess.run(shell=True)` is forbidden in production code (bandit S602). If you need shell semantics, go through `coda.sandbox.shell.run()` which validates against the dangerous-command policy.
- Test layout: `tests/unit/` mirrors `src/coda/`; `tests/integration/` for cross-module tests; `tests/e2e/` for full-loop tests with VCR-recorded LLM responses.
- Coverage target: ≥ 90% for `coda.core` / `coda.llm`; 100% for `coda.tools` / `coda.sandbox`.
- All new tools must:
  1. Define a `pydantic.BaseModel` for arguments.
  2. Return a `ToolResult` (never raise to the loop).
  3. Mark `requires_approval` correctly (default = read-only, write/exec = True).
  4. Have a corresponding entry in `tests/unit/tools/test_<tool>.py` and a recorded VCR cassette in `tests/e2e/`.

## Module dependency rules (enforced via `import-linter` in CI)

- `core` may depend on `llm` / `tools` / `sandbox` / `memory` / `obs`. Never the reverse.
- `tools` may depend on `sandbox` / `memory` / `obs`. Never on `core`.
- `cli` and `tui` are thin layers over `core`'s facade — they never construct messages or call LLMs directly.

## Common commands

```bash
uv sync                        # install / sync deps
uv run pytest                  # all tests
uv run pytest tests/unit       # unit only (fast)
uv run pytest --cov-report=html # coverage report → htmlcov/
uv run ruff check . --fix      # lint + autofix
uv run mypy src                # type-check
uv run python scripts/prototype.py    # Phase 0 prototype (when written)
```


## M2 tools: the 8 new tools added in Phase 1 M2

M2 extended the 3 M1 tools to a full set of 11:

### Search / read tools (no approval needed)
- **`list_dir`** — list directory with depth control (1–10). Skips noise dirs (node_modules, __pycache__, .git, .venv, etc.).
- **`glob`** — find files by pathlib glob pattern. Filters via `filter_allowed_paths()` (symlink-safe).
- **`grep`** — regex search in files. Prefers ripgrep (`rg`) for speed; Python `re` fallback. Install ripgrep for large codebases.

### Edit / patch tools (require approval in auto_edit mode)
- **`edit_file`** — precise string replacement. `old_string` must be unique unless `replace_all=true`. On ambiguity, reports line numbers to help the model narrow context.
- **`apply_patch`** — apply a unified diff (udiff). Atomic transaction: snapshots all targets before writing; rolls back all on any failure. Handles LF/CRLF transparently.

### Git tools (status/diff = no approval; commit = approval)
- **`git_status`** — `git status --porcelain` via GitPython. Error if not in a git repo.
- **`git_diff`** — unified diff; supports `staged=true` and path filter.
- **`git_commit`** — commit staged files. `add_all=true` runs `git add -u` first. Auto-redacts API key literals from commit messages.

### Path safety (all tools)
`sandbox/path_filter.py` provides `filter_allowed_paths()` and `is_noise_dir()`, used by bulk-result tools to silently drop out-of-workspace paths and noise directories.

### Approval modes (M2 complete)
- `read_only` — only read/search/status tools; write/shell always denied.
- `auto_edit` (default) — reads auto-approved; writes prompt `[y/n/a/p/?]`.
  - `p` enters a pattern rule: `<tool_name> <glob>` (e.g. `run_shell pytest*`).
  - Pattern rules stored in memory; persisted via `JsonlSessionStore` in M5.
- `full_auto` — all tools auto-approved; red warning banner printed at startup.

## M3: Token budget, dual compression, and error recovery (Phase 1 M3)

M3 brings context-window safety and centralised error recovery to the agent loop.

### Token budget (§3.4.1)

`src/coda/core/budget.py` — `BudgetCalculator`:
- Total budget = model context window × 0.80
- Output reserve = total_budget × 0.15
- `check(messages)` → `BudgetStatus(action: BudgetAction)`: OK / COMPRESS / TRUNCATE / REFUSE
- `MODEL_CONTEXT_WINDOW` table covers ~20 common models (conservative 95% values)
- `CODA_TOKEN_RATIO` env var overrides the per-model ratio (Claude = 1.1× default)

### Dual compression (`CODA_COMPRESS`)

`src/coda/core/compression.py` — `make_compressor(strategy, provider)`:
- `llm` (default): calls the same LLMProvider to summarise oldest N rounds into a `<SUMMARY>` block; emits `SessionEvent(COMPRESSION)` with cost.
- `algorithmic`: drops `tool_result` content; keeps tool-call metadata + file paths; zero extra LLM cost.
- **Pinned context**: most-recent 5 file paths from tool_call args + last user message are **never** compressed.
- Compression is triggered before each `provider.chat()` call (§4.9 of M3 plan).

### Error recovery (`src/coda/core/recovery.py`)

`handle(RecoveryContext) -> (RecoveryAction, str)` dispatches 7 scenarios:

| # | `error_kind` | `RecoveryAction` | Behaviour |
|---|-------------|-----------------|-----------|
| 1 | `bad_json` | RETRY / ABORT | Re-inject schema+error; retry ×2 |
| 2 | `tool_timeout` | SKIP | Kill; inject partial-result stub |
| 3 | `stall` | ABORT | Print last 3 calls; abort turn |
| 4 | `context_loss` | RETRY | Re-inject pinned file paths |
| 5 | `sha256_conflict` | SKIP | Block write; ask for re-read |
| 6 | `provider_error` | RETRY / ABORT | Exp backoff 1s/2s/4s ×3 |
| 7 | `eacces` | SKIP | Report path + permission bits |

`StallDetector`: tracks write-tool-call signatures; raises `StallError` at 3 consecutive identical calls (read-only tools excluded).

### SHA-256 conflict detection (scenario 5)

`read_file` caches the SHA-256 of the file at read-time in a module-level `_sha256_cache`.  `edit_file` and `apply_patch` validate the cache before writing; if the on-disk hash differs, they return an error message asking the agent to re-read the file.

### SessionEvent stream (`src/coda/core/events.py`)

`SessionEventLogger` wraps the existing JSONL logger with:
- Typed `emit(SessionEventType, data)` → `SessionEvent`
- `emit_from(event)` — for compressor-generated events (overwrites session_id + seq)
- Monotonically-increasing `seq` counter
- Session events: `<workspace>/.coda/sessions/<session_id>.jsonl`
- App logs: `<workspace>/.coda/logs/<session_id>.log`
- Factory: `SessionEventLogger.from_store(session_id, store)` (preferred) or `from_workspace_path` (legacy)

Events emitted by AgentLoop: `USER_MESSAGE`, `ASSISTANT_MESSAGE`, `TOOL_CALL`, `APPROVAL_DECISION`, `TOOL_RESULT`, `COMPRESSION`, `ERROR`.

### New CLI flags (M3)

- `--max-tool-calls N` — tool calls per turn limit (default 15)
- `--summary-window N` — dialogue rounds to compress in one pass (default 2)
- Startup banner now shows: model context window / compression strategy / max tool calls

## M4: `.codaignore`, Git checkpoint, `coda undo`, diff preview

### CodaIgnore (`src/coda/sandbox/ignore.py`)

`CodaIgnore` merges 4 tiers of ignore rules (backed by `pathspec` gitignore semantics):

| Tier | Source | Always active? |
|------|--------|----------------|
| 1 | Hard-coded defaults: `.env`, `*.pem`, `id_rsa`, `node_modules/`, `__pycache__/`, etc. | ✅ yes |
| 2 | Project `.gitignore` | — |
| 3 | `<workspace_root>/.codaignore` | — |
| 4 | `~/.config/coda/codaignore` | — |

**API**: `ignore.match(path) -> MatchResult`; `ignore.is_ignored(path) -> bool`.

All read tools (`read_file`, `list_dir`, `glob`, `grep`) check `ctx.ignore.match()` before accessing the path.  On match, they return `PathIgnoredError: '<path>' is ignored (rule: '<pattern>' from <source>)` as `ToolResult(is_error=True)`.

One `CodaIgnore` instance is constructed at session start and injected via `ToolContext.ignore`.

### GitCheckpointManager (`src/coda/sandbox/checkpoint.py`)

`GitCheckpointManager.create(affected_paths) -> str | None`:
- Runs `git stash create` (does **not** push to stash stack; working tree untouched).
- Returns the stash SHA on success, `None` on clean tree or non-git workspace.
- Logs a warning and returns `None` on non-git workspaces (no crash).

`GitCheckpointManager.restore(sha, paths)`:
- Runs `git checkout <sha> -- <paths>` (only touches the listed paths).
- Raises `CheckpointError` on non-git workspace or bad SHA.

`shell_command_writes(cmd) -> bool`: heuristic that returns `True` if *cmd* likely modifies the filesystem (redirect `>`, `rm`, `mv`, `sed -i`, `tee`, `wget`, etc.).

Write tools wire-up:
- `write_file`, `edit_file` → `create([target_path])` before writing.
- `apply_patch` → `create(all_affected_paths)` before applying.
- `run_shell` → `create([workspace_root])` when `shell_command_writes()` returns `True`.

Each checkpoint emits a `CHECKPOINT` `SessionEvent` via `ctx.event_logger`.

### `coda undo` (`src/coda/cli/undo.py`)

Typer subcommand registered in `main.py`:

```bash
coda undo [--root <workspace>] [--session <session_id>]
```

Behaviour:
1. Find the session JSONL (`<workspace>/.coda/logs/<session_id>.jsonl`; defaults to most-recent).
2. Find the latest `CHECKPOINT` event (by `seq`).
3. Call `GitCheckpointManager.restore(sha, affected_paths)`.
4. Emit `UNDO` `SessionEvent`.
5. Non-git workspace or no CHECKPOINT → exit 1 with friendly error.
6. If `affected_paths == [workspace_root]` (shell command scope) → prompt for confirmation.

### Diff preview (`src/coda/cli/diff_preview.py`)

`render_diff(old, new, path) -> rich.syntax.Syntax`: returns a Rich `Syntax` object showing a unified diff, truncated to 200 lines.

`render_new_file(content, path) -> Syntax`: convenience wrapper for new files (`old=None`).

`TerminalApprovalManager._maybe_render_diff()` renders diffs for `write_file`, `edit_file`, `apply_patch` before the `y/n` approval prompt in `auto_edit` mode.

### Banner update (`src/coda/cli/banner.py`)

Session banner now shows a `git: <root> | none` line reflecting `workspace.git_root`.

### New dependency

`pathspec>=0.12,<1` added to `pyproject.toml` (major version pinned per engineering rule 7).

## M4.9: Interactive REPL (multi-turn dialogue)

The CLI now has two entry shapes — both share the *same* `AgentLoop`
instance, so history persists naturally:

```bash
coda "one-shot task"     # headless: run once and exit (unchanged)
coda                     # REPL: read → run → repeat
```

### Implementation map

- `src/coda/cli/main.py`
  - `_build_session_components(...)` → returns a `SessionComponents`
    bundle (workspace + AgentLoop + logger + session_id + event_logger
    + log_path + max_tokens).
  - `_run_headless(prompt, components)` → one `loop.run` + headless
    summary (preserves pre-M4.9 behaviour byte-for-byte).
  - `ABORT_REASONS` / `_echo_turn_result` / `_collect_written_paths`
    are module-level so the REPL can reuse them.
- `src/coda/cli/repl.py`
  - `run_repl(components)` — the multi-turn loop.
  - `input()` runs in `asyncio.to_thread` so it doesn't block the event loop.
  - Exit tokens: `exit` / `quit` / `:q` / `\q`; Ctrl-D (EOFError) exits
    immediately; Ctrl-C at an empty prompt requires a second press
    (`last_ctrl_c` sentinel, mirrors Python/IPython UX).
  - Ctrl-C *during* a turn cancels just that turn (does **not** exit REPL).
  - Empty / whitespace-only input is silently skipped.
  - Session summary is printed exactly once at exit (`print_session_summary`
    in `main.py`); per-turn output is just the model answer or abort message.

### What carries across REPL turns

| State | Lifetime |
|---|---|
| `session_id` / `SessionEventLogger` / JSONL log path | whole REPL session |
| `GitCheckpointManager` | whole REPL session (checkpoints accumulate) |
| `AgentLoop.context_manager` (conversation history) | whole REPL session |
| `StallDetector` / `tool_calls_made` / `invalid_args_retry` | per turn (reset by `loop.run`) |
| Banner / `full_auto` warning | printed once at session start |

### What's deliberately deferred to M6

- `prompt_toolkit` upgrade (arrow-key history, multi-line, syntax highlighting)
- Slash commands (`:undo`, `:tokens`, `:clear`, `/compact`)
- Cancelling in-flight LLM streaming on single Ctrl-C (requires provider-level
  cancellation)

## M5: Persistence + memory (JSONL sessions, `coda resume`, AGENTS.md, config)

### JsonlSessionStore (`src/coda/memory/store.py`)

`JsonlSessionStore` implements the `SessionStore` Protocol backed by one `.jsonl` file per session in `<workspace>/.coda/sessions/`.

- `create_session(session_id, workspace, model, agents_md_hash)` → writes `SESSION_INIT` as `seq=0`.
- `append_event(session_id, event)` → appends a single JSONL line.
- `load_events(session_id)` → reads all events in order.
- `max_seq(session_id)` → reads only the **last line** for O(1) seq lookup.
- `list_recent(n)` → reads the `SESSION_INIT` header from each file to build `SessionRow` summaries.

`SessionRow` is a lightweight dataclass (`session_id`, `workspace`, `model`, `started_at`, `event_count`) returned by `list_recent`.

### `coda resume` (`src/coda/cli/resume.py`)

```bash
coda resume --list                 # show recent sessions
coda resume <id-or-prefix>         # replay + continue in REPL
coda resume --root /path <id>      # explicit workspace
```

Internals:
1. `_resolve_session_id(store, token)` — exact match or unique prefix match; error on ambiguity.
2. `store.load_events(session_id)` — load all stored events.
3. `replay_events(events, context_manager)` — reconstruct `InMemoryContextManager._history` from `USER_MESSAGE`, `ASSISTANT_MESSAGE`, `TOOL_RESULT`, and `COMPRESSION` events.
4. Drop into the REPL with the restored history.

`ReplayStats` (`src/coda/memory/replay.py`) tracks `messages_restored`, `tool_results_restored`, and `compressed` (bool).

### AGENTS.md 3-tier merge (`src/coda/memory/agents_md.py`)

`load_agents_md(workspace, cwd) -> AgentsMdBundle` collects and merges:

| Tier | Path | Droppable on overflow? |
|------|------|----------------------|
| 1 (system) | `~/.config/coda/AGENTS.md` | Yes |
| 2 (project) | `<workspace>/AGENTS.md` | **No** — always kept |
| 3 (subdir) | `<cwd>/AGENTS.md` … up to workspace root | Yes (outermost first) |

Rules:
- Per-file limit: **8K tokens** (truncated with `…[truncated]` suffix).
- Total limit: **12K tokens** (tiers 1 and 3 are dropped first).
- The bundle's SHA-256 hash is stored in the `SESSION_INIT` event.
- Content is injected as a `<project_memory>…</project_memory>` user message at index 0 of `_history`.

### Config loading (`src/coda/core/config.py`)

`CodaConfig` (Pydantic model) covers `model`, `approval`, `max_tool_calls`, `summary_window`, `compress`, `token_ratio`.

`load_config(workspace_root) -> tuple[CodaConfig, list[tuple[Path, str]]]` merges:

1. `~/.config/coda/config.toml` (user, TOML)
2. `<workspace>/.coda/config.yaml` (workspace, YAML — takes precedence over user)

Precedence chain: **CLI flag > env var > workspace YAML > user TOML > built-in default**.

`main.py` applies config values to Typer options only when `ParameterSource` (from `click.core`) shows the option is still at its default — CLI flags and env vars always win.

`coda doctor` now shows a **Config sources** section listing which files were found and which keys they contribute.

## M6: streaming, cost, stdin, slash commands, approval persistence

### Streaming output (`src/coda/llm/streaming.py`, M6.1)

`ChunkAccumulator` reassembles `LLMChunk`s into a complete assistant `Message`: text deltas are concatenated, tool-call fragments are merged by index (the `arguments` JSON string arrives split across chunks and is `json.loads`-ed at the end, falling back to `{"_raw": ...}` on malformed JSON so the BAD_JSON recovery path still triggers), and the final `usage` / `finish_reason` are captured.

`AgentLoop._call_llm` streams when **both** `LoopConfig.stream` (default True) and the provider's `supports_streaming` attribute are truthy; otherwise it falls back to non-streaming `chat()`. Test mocks don't set the attribute, so they transparently use `chat()` — zero test churn. Text deltas go through the constructor-injected `on_delta` callback (default: Rich raw print, no markup). `TurnResult.streamed` tells `_echo_turn_result` not to print `final_text` a second time.

### Cost tracking (`src/coda/obs/cost.py`, M6.2 — engineering rule #4)

- `Message.usage` (`{prompt_tokens, completion_tokens, total_tokens}`) and `Message.cost_usd` are filled by `LiteLLMProvider.chat()` via `response.usage` + `litellm.completion_cost`; the streaming path gets usage from the final chunk and the loop estimates cost with `litellm.cost_per_token`. Unknown models: tokens tracked, cost `None`.
- `CostTracker` accumulates per-session totals; `AgentLoop` emits one `COST_SNAPSHOT` event per turn with `{turn_*, total_*}` token/cost fields.
- Both session summaries print `tokens     : 12.3k in / 4.1k out | cost $0.0231` (cost omitted when unknown). Replay skips `COST_SNAPSHOT`.

### Pipe stdin entry (M6.3)

`echo "fix the bug" | coda` runs headless with the piped text as the prompt. `git diff | coda "review this"` appends stdin as a `<stdin>...</stdin>` context block after the prompt. Empty piped stdin (CliRunner test streams) keeps the REPL behaviour — this completes task 1.10's three-entry acceptance (REPL / exec / pipe).

### REPL slash commands (M6.4)

Handled locally in `repl.py:_dispatch_slash` — the LLM never sees them. Checked after the exit-token test, so `:q` still exits.

| Command | Action |
|---------|--------|
| `:help` | list commands |
| `:sessions` | recent-sessions table (shared `render_sessions_table` with `resume --list`) |
| `:undo` | `undo_command(...)` with `typer.Exit` caught so the REPL survives |
| `:cost` | CostTracker totals |
| `:resume <id>` | switch session: `run_repl` returns the target id; `repl_session_cycle` (resume.py) rebuilds components, replays history, re-enters |

### Approval trust persistence (M6.5)

`TerminalApprovalManager.export_state()/restore_state()` snapshot `_session_trusted` + `_pattern_trust`. When a decision is `approve_session`/`approve_pattern`, the `APPROVAL_DECISION` event carries the full `state` snapshot (last one wins). `replay_events(events, ctx, approval=...)` re-applies the latest snapshot on resume, so `a`/`p` answers survive `coda resume`. Cross-session global policy stays Phase 3 (`policy.toml`).

## When you (the agent) modify this codebase

- Always `read_file` before `edit_file` — never guess file contents.
- Before any write, describe what you intend to do in plain language.
- After changes, run the relevant test suite (not the full suite — pick what's affected).
- If `mypy --strict` fails, fix it in the same edit. Do not push type errors.
- Use `git_diff` to verify your change before declaring success; do not assume the file was written correctly.
- Never modify `pyproject.toml` dependency versions without explicit user approval (rule 7).
- Never modify `LICENSE`, `.gitignore`, or `.github/` without explicit user approval.
