# Project memory: Coda

> This file is auto-loaded into the system prompt of every Coda session running in this repository (see `docs/architecture.md` §5.6). Keep it concise — total budget is 8K tokens.

## What this project is

Coda is a local-first, multi-provider coding agent CLI. We are currently in **Phase 1 (MVP)**: building a usable REPL + headless `coda exec` with 11 tools, three approval modes, automatic git checkpoints, and SQLite-backed sessions. We are explicitly **not** building TUI / RAG / Docker sandbox / IDE plugins / Web UI in this phase.

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
  - Pattern rules stored in memory; persisted to SQLite in M5.
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
- JSONL path: `<workspace>/.coda/logs/<session_id>.jsonl`
- Factory: `SessionEventLogger.from_workspace_path(session_id, workspace_root)`

Events emitted by AgentLoop: `USER_MESSAGE`, `ASSISTANT_MESSAGE`, `TOOL_CALL`, `APPROVAL_DECISION`, `TOOL_RESULT`, `COMPRESSION`, `ERROR`.

### New CLI flags (M3)

- `--max-tool-calls N` — tool calls per turn limit (default 15)
- `--summary-window N` — dialogue rounds to compress in one pass (default 2)
- Startup banner now shows: model context window / compression strategy / max tool calls

## When you (the agent) modify this codebase

- Always `read_file` before `edit_file` — never guess file contents.
- Before any write, describe what you intend to do in plain language.
- After changes, run the relevant test suite (not the full suite — pick what's affected).
- If `mypy --strict` fails, fix it in the same edit. Do not push type errors.
- Use `git_diff` to verify your change before declaring success; do not assume the file was written correctly.
- Never modify `pyproject.toml` dependency versions without explicit user approval (rule 7).
- Never modify `LICENSE`, `.gitignore`, or `.github/` without explicit user approval.
