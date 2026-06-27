# Changelog

All notable changes to Krodo are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
once v0.1.0 is tagged.

## [Unreleased]

### Added
- M7 release scaffold: QUICKSTART, CONTRIBUTING, SECURITY (this changelog).

### Changed
- **Brand rename: Coda → Krodo.** Distribution name, Python import package,
  CLI command, workspace state directory (`.coda/` → `.krodo/`), config
  directory (`~/.config/coda/` → `~/.config/krodo/`), and environment
  variables (`CODA_*` → `KRODO_*`) all renamed. Reason: PyPI `coda` occupied
  by an unrelated 2017 file-tagging package; GitHub `agno-agi/coda` is an
  active same-category coding-agent project. Krodo is verified clean across
  PyPI / npm / `.ai` / `.dev` / `.com`.

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

[Unreleased]: https://github.com/wadekun/krodo/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/wadekun/krodo/releases/tag/v0.1.0
