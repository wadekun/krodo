# Changelog

All notable changes to Krodo are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
once v0.1.0 is tagged.

## [Unreleased]

### Added
- Krodo is now on PyPI: `uv tool install krodo` / `pipx install krodo`.
  Auto-published via Trusted Publishers (OIDC) on `release.published`.
- **Provider matrix CI job** (Phase 2 M8). `.github/workflows/ci.yml` now
  runs a one-turn smoke request against Anthropic / OpenAI / Gemini / DeepSeek / Z.AI (GLM) on
  every non-draft PR. `continue-on-error: true` so a flaky API or quota
  issue never blocks merge; steps auto-skip when the repo secret is unset,
  so the job is green even before keys are configured. Smoke script:
  `.github/workflows/scripts/provider_e2e.py`.
- **Anthropic prompt caching** (Phase 2 M8). `LiteLLMProvider` tags the
  system message with `cache_control: {"type": "ephemeral"}` for
  `anthropic/*` models, so the static prompt prefix is cached across turns
  (lower latency + cost on long sessions). Defaults on; opt out via
  `prompt_cache: false` in `.krodo/config.yaml`. No-op for OpenAI/Gemini
  (they cache provider-side automatically).
- **tree-sitter symbol index** (Phase 2 M9). New `src/krodo/indexer/` module
  extracts symbol definitions/references (Python / JavaScript / TypeScript /
  Go) into a SQLite index (`SymbolBackend` Protocol + `TreeSitterSymbolIndex`
  impl). This is the data foundation for M10 (repo-map) and M11 (symbol
  tools) — no tools are registered yet. Built once at session start
  (incremental across sessions via stored mtime+size); an `INDEX_BUILD` event
  is recorded and `krodo doctor` shows the stats. New config key
  `symbol_backend` (`treesitter` default, `off` to disable; `lsp` reserved for
  Phase 3).
- **Write-tool index invalidation** (Phase 2 M9). `write_file` / `edit_file` /
  `apply_patch` invalidate the index for the files they touch after a
  successful write; the next query re-extracts only those files, so a renamed
  symbol is visible immediately without a rebuild.
- **Repo-map context injection** (Phase 2 M10). New `src/krodo/memory/repo_map.py`
  builds an Aider-style file reference graph from the M9 symbol index (edges:
  referencing file → defining file, name-based approximation), ranks files
  with a handwritten deterministic PageRank, and renders a directory-grouped
  signature tree into a token budget. The map is injected as a `<repo_map>`
  context message right after `<project_memory>` and refreshed before each
  turn only when the index actually changed (monotonic index `version` gate;
  byte-identical re-renders keep the old bytes to preserve the prompt cache).
  New config keys: `repo_map` (default on when the index is on) and
  `repo_map_tokens` (default 2048). Perf: ha-core (18k files / 176k symbols /
  644k refs) renders in ~3s, the krodo repo itself in 29ms; renders fire only
  on index change.
- **Second Anthropic cache breakpoint on the stable prefix** (Phase 2 M10).
  `LiteLLMProvider` now also tags the last stable-prefix message (`<repo_map>`
  when present, else `<project_memory>`) with `cache_control`, so the whole
  static prefix — system prompt + AGENTS.md memory + repo-map — caches as one
  unit instead of only the system message.

### Changed
- (No unreleased changes.)

### Fixed
- **`<project_memory>` no longer evicted under context pressure** (Phase 2 M10).
  Compression pinning claimed to protect the project-memory message but never
  did: after the first real user turn it could be folded into an LLM summary,
  and the hard-truncation fallback unconditionally popped the oldest message —
  dropping `<project_memory>` first. `_pinned_ids` and hard truncation now
  both protect the stable prefix (`<project_memory>` / `<repo_map>`), evicting
  the oldest non-prefix message instead.
- **`tree-sitter` pinned to `<0.26`** (Phase 2 M9). `tree-sitter` 0.26.0 has a
  `Point.row`/`Point.column` reference-counting bug ([py-tree-sitter#466](https://github.com/tree-sitter/py-tree-sitter/pull/466),
  merged upstream but not yet released) that frees the backing int too early;
  under enough non-cached (>256) `Point` reads in one process — exactly what
  symbol extraction does on any real multi-symbol file — the freed memory gets
  reused and a later read segfaults (SIGSEGV). Reproduced deterministically on
  real-world Python files (e.g. `httpx`, ~40% of files) on macOS arm64, Linux
  arm64, and Linux amd64; narrowing the pin to `tree-sitter>=0.25,<0.26` is the
  upstream-confirmed workaround (see [py-tree-sitter#472](https://github.com/tree-sitter/py-tree-sitter/issues/472)).
  Since `symbol_backend` defaults to `treesitter`, any fresh install on 0.26.0
  would crash at session start while building the index — this is not a
  platform-specific edge case. Full diagnosis in
  `docs/benchmarks/m9_symbol_index_perf_results.md`. Once py-tree-sitter
  releases a version containing #466, the pin can be relaxed to `<0.27` after
  re-verifying with the `scan.py` benchmark script.

## [0.1.1] — 2026-06-28

### Added
- `krodo --version` / `-V` flag — prints version + exits 0. Eager callback
  on the main Typer app, so it fires before any other option or subcommand
  dispatch. `__version__` centralised in `src/krodo/__init__.py` via
  `importlib.metadata` (with `"dev"` fallback for source-only runs).
- Banner now shows the resolved model string in its own row (between
  `git` and `approval`), so users can verify config at a glance.
- "Thinking…" spinner in REPL and headless modes between user prompt and
  the first streamed LLM token. `AgentLoop.run()` gains a per-turn
  `on_first_token` callback; both streaming and non-streaming LLM paths
  fire it once before the first rendered delta. The CLI uses Rich's
  `console.status()` to drive the spinner and stops it on first token via
  the callback (with a `finally` safety net for error paths).
- PyPI Trusted Publishers workflow (`.github/workflows/publish.yml`).
  Triggers on `release.published`; also has `workflow_dispatch` fallback
  because GitHub suppresses `published` events when a release is deleted
  + re-created with the same tag.

### Fixed
- Approval prompt now shows the right per-tool context. The hardcoded
  `"path"` / `"cmd"` lookup was missing `run_shell` (uses `command`),
  `git_commit` (uses `message`), and `apply_patch` (uses `patch`).
  Replaced with a priority-keyed `_HINT_KEYS` tuple (`path > command >
  message`) and a `_tool_call_hint()` helper. `patch` is intentionally
  omitted because `_maybe_render_diff` already renders the full diff
  below the prompt.
- `.krodo/config.yaml` field names documented in MODELS.md now match
  the actual `KrodoConfig` Pydantic model (`model` not `provider`;
  `approval` not `approval_mode`; `max_tool_calls` not
  `max_tool_calls_per_turn`). Pydantic silently ignores unknown fields.
- `krodo doctor` now respects workspace config model (was hardcoded
  to `_DEFAULT_MODEL`). Mirrors the same "config overrides default"
  logic in `main.py` and `resume.py`.
- LiteLLM issue #14011 (`PydanticSerializationUnexpectedValue:
  ServerToolUse`) warning silenced via a targeted `warnings.filterwarnings`
  call. Filter scope is narrow (regex on the message + `UserWarning`
  category only) so unrelated warnings still surface.
- M7 rename cleanup: `.gitignore` patterns (`.coda/*` → `.krodo/*`),
  `LICENSE` (`The Coda Contributors` → `The Krodo Contributors`), and
  the physical `.coda/` directory on disk were missed by the initial
  perl pass.
- CI failure on first GitHub push: 10 pre-existing files needed
  `ruff format`; correct repo owner `liangck` → `wadekun` across docs.

### Fixed
- mypy `--strict` now passes with zero errors (was 10 errors before M7).
  Root causes: missing `types-PyYAML` stubs, `int(object)` overload failures
  in `LLMChunk.usage` parsing, bare `dict`/`list` generics in two CLI helpers.

## [0.1.0] — Phase 1 milestones M1–M6 (collected changelog)

Krodo v0.1.0 is the Phase 1 feature-complete release. It ships a usable
REPL + headless CLI with 11 tools, three approval modes, automatic git
checkpoints, JSONL-backed sessions, and AGENTS.md project memory. The
sections below summarise each milestone; full design notes live in
[`docs/architecture.md`](docs/architecture.md) §10 changelog.

### M1 — Walking skeleton
- Single-turn ReAct loop (`krodo/core/loop.py`), 3 tools (`read_file`,
  `write_file`, `run_shell`), Typer CLI entry, `ToolRegistry` with
  auto-generated LiteLLM JSON schemas.

### M2 — Full tools + three approval modes
- 8 more tools: `list_dir`, `glob`, `grep`, `edit_file`, `apply_patch`,
  `git_status`, `git_diff`, `git_commit`. Total 11.
- `TerminalApprovalManager` with `read_only` / `auto_edit` (default) /
  `full_auto` modes. `auto_edit` prompts `y/n/a/p/?` and supports
  pattern trust (e.g. `run_shell pytest*`).
- Path firewall + dangerous-command blocklist + symlink-safe
  `filter_allowed_paths()`.

### M3 — Token budget, dual compression, error recovery
- `BudgetCalculator` triggers compression at 80% context window.
- `make_compressor("llm"|"algorithmic")`: LLM summarises oldest N rounds
  into `<SUMMARY>` block; algorithmic drops `tool_result` content but
  keeps metadata. Pinned context (recent 5 file paths + last user
  message) is never compressed.
- `recovery.py` covers 7 scenarios: `bad_json`, `tool_timeout`, `stall`,
  `context_loss`, `sha256_conflict`, `provider_error`, `eacces`.
- `StallDetector` aborts the turn after 3 consecutive identical write calls.
- `SessionEventLogger` emits typed JSONL events for replay and observability.
- New CLI flags: `--max-tool-calls`, `--summary-window`.

### M4 — `.krodoignore`, git checkpoint, `krodo undo`, diff preview
- `.krodoignore` (4-tier merge): hard-coded defaults + project `.gitignore`
  + `<workspace>/.krodoignore` + `~/.config/krodo/krodoignore`. Backed by
  `pathspec` gitignore semantics.
- `GitCheckpointManager.create()` runs `git stash create` before every
  write (does NOT touch the stash stack).
- `krodo undo` restores files via `git checkout <sha> -- <paths>`.
- Diff preview rendered before `y/n` approval in `auto_edit` mode.

### M4.5–M4.9 — UX polish and interactive REPL
- LiteLLM noise silenced; abort reason surfaced to user; Windows `glm-*`
  compatibility; `krodo doctor` diagnostics command.
- Dynamic tool list injected into system prompt; configurable `--max-tokens`.
- `stop_reason` diagnostic logging; invalid-args retry budget.
- **Interactive REPL** (`krodo` with no prompt): multi-turn dialogue with
  persistent conversation history. Exit via `exit`/`quit`/`:q`/Ctrl-D/two
  Ctrl-C. Ctrl-C during a turn cancels just that turn.

### M5 — Persistence + memory
- `JsonlSessionStore` writes `<workspace>/.krodo/sessions/<id>.jsonl`
  (one event per line, monotonic `seq`).
- `krodo resume [--list] [<id-prefix>]` replays stored events into
  `InMemoryContextManager`, restoring full conversation context.
- AGENTS.md 3-tier merge: system (`~/.config/krodo/AGENTS.md`) + project
  (`<workspace>/AGENTS.md`, always kept) + subdir walk. 8K per-file,
  12K total budget, SHA-256 stored in `SESSION_INIT`.
- Config loading: `~/.config/krodo/config.toml` + `<workspace>/.krodo/config.yaml`.
  Precedence: CLI flag > env > workspace YAML > user TOML > default.

### M6 — Streaming, cost, pipe stdin, slash commands, approval persistence
- **M6.1 streaming**: `ChunkAccumulator` reassembles LiteLLM chunks;
  `AgentLoop._call_llm` streams when provider declares `supports_streaming`;
  falls back to `chat()` for mocks.
- **M6.2 cost tracking**: `Message.usage` / `cost_usd` filled by provider;
  `CostTracker` accumulates session totals; one `COST_SNAPSHOT` event per
  turn; session summary prints `tokens: Xk in / Yk out | cost $Z`.
- **M6.3 pipe stdin**: `echo task | krodo` runs headless with stdin as
  prompt; `git diff | krodo "review this"` adds stdin as `<stdin>` context.
- **M6.4 REPL slash commands**: `:help` / `:sessions` / `:undo` / `:cost` /
  `:resume <id>`. Handled locally, never sent to the LLM.
- **M6.5 approval trust persistence**: `approve_session` / `approve_pattern`
  decisions survive `krodo resume` via state snapshots in `APPROVAL_DECISION`
  events.
- Tool-call-limit interrupt now synthesises a `[skipped: tool call limit
  reached]` tool_result so `krodo` can `continue_turn()` mid-batch without
  losing the LLM's pending work.

### M7 — Release preparation
- mypy strict clean (see Fixed above).
- Brand rename to Krodo (see Changed above).
- Documentation quartet (QUICKSTART / CONTRIBUTING / SECURITY / this file).
- GitHub repo + v0.1.0 tag + Release (deferred until owner confirms).

[Unreleased]: https://github.com/wadekun/krodo/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/wadekun/krodo/releases/tag/v0.1.1
[0.1.0]: https://github.com/wadekun/krodo/releases/tag/v0.1.0
