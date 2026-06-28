# Krodo

[![CI](https://github.com/wadekun/krodo/actions/workflows/ci.yml/badge.svg)](https://github.com/wadekun/krodo/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![Status: Pre-alpha](https://img.shields.io/badge/status-pre--alpha-orange.svg)](CHANGELOG.md)

> A local-first, multi-provider **coding agent CLI**, built with Python 3.12+.
>
> Status: 🚧 **Pre-alpha (v0.1.0)** — Phase 1 feature-complete (REPL + headless + pipe, 11 tools, JSONL sessions, three approval modes). Phase 2 (TUI, MCP client, tree-sitter symbol index) in planning.

Krodo is an open-source coding agent inspired by Claude Code, Codex CLI, and Aider. It runs locally, talks to your codebase through tools (read / edit / shell / git / grep), and supports any LLM provider via [LiteLLM](https://github.com/BerriAI/litellm) — Anthropic, OpenAI, Gemini, DeepSeek, Qwen, plus local models via Ollama / vLLM.

## Why another coding agent

- **Local-first**: your code never leaves your machine except for LLM API calls.
- **Multi-provider from day 1**: switch between Claude, GPT, Gemini, DeepSeek, Qwen, or local models with a single config flag.
- **Three CLI shapes, one core**: `krodo` REPL, `krodo "<prompt>"` headless, `krodo tui` (Phase 2) — all share the same agent loop.
- **Safety as a default**: three approval modes (`read_only` / `auto_edit` / `full_auto`), path firewall, dangerous-command blocklist, automatic git checkpoint before every write.
- **Modular monolith**: clean Protocol-based interfaces between `core` / `llm` / `tools` / `sandbox` / `memory` / `obs`. Easy to read, easy to contribute to.

For full design rationale, see [`docs/architecture.md`](docs/architecture.md).

## Roadmap

| Phase | Scope | Status |
|------:|:------|:------:|
| 0 | Single-file prototype validating the ReAct loop | ✅ done |
| 1 M1 | Walking skeleton (3 tools, CLI, agent loop) | ✅ done |
| 1 M2 | Full tools (11 tools) + three approval modes + pattern trust | ✅ done |
| 1 M3 | Context management (token budget + dual compression) + 7 recovery scenarios | ✅ done |
| 1 M4 | `.krodoignore` + git checkpoint + `krodo undo` + diff preview | ✅ done |
| 1 M5 | Persistence + memory: JSONL sessions, `krodo resume`, AGENTS.md, config files | ✅ done |
| 1 M6 | Streaming + cost tracking + pipe stdin + REPL slash commands + approval persistence | ✅ done |
| 1 M7 | Brand rename (Coda → krodo) + mypy strict clean + docs quartet + GitHub release + dogfood PR | ✅ done |
| 2 | tree-sitter symbol index, repo-map, Textual TUI, MCP client | — |
| 3 | OS-level sandbox, evaluation harness, OpenTelemetry / Langfuse | — |
| 4 | Production-grade: Rust hot paths, single-binary distribution, LiteLLM Proxy | — |

## Quick start

### Try the v0.1.0 release (Phase 1 feature-complete)

```bash
git clone https://github.com/wadekun/krodo
cd krodo
uv sync

export ANTHROPIC_API_KEY=sk-ant-...   # or OPENAI_API_KEY / KRODO_API_KEY

mkdir -p /tmp/krodo-sandbox

# Headless: run one task and exit
uv run krodo --root /tmp/krodo-sandbox "create hello.py that prints Hello Krodo, then run it"

# REPL: omit the prompt to enter interactive multi-turn mode
uv run krodo --root /tmp/krodo-sandbox
# you> create a simple mario game
# (assistant works, then…)
# you> now add a sound effect when collecting coins
# you> exit          # or Ctrl-D / Ctrl-C twice

# Pipe: stdin becomes the prompt…
echo "create hello.py that prints Hello Krodo" | uv run krodo --root /tmp/krodo-sandbox

# …or extra context when a prompt is given
git diff | uv run krodo "review this change for bugs"
```

Assistant text **streams token-by-token**, and every session summary ends with a
cost line like `tokens     : 12.3k in / 4.1k out | cost $0.0231`.

In REPL mode the conversation history (including everything the agent did
in the previous turn) is carried over, so follow-ups like "now add X" or
"fix the bug from before" work naturally.  Exit with `exit` / `quit` /
`:q`, Ctrl-D, or two consecutive Ctrl-C presses at the prompt.

### REPL slash commands

Slash commands are handled locally — they are never sent to the LLM:

| Command | Action |
|:--------|:-------|
| `:help` | List available commands |
| `:sessions` | Show the 10 most recent sessions in this workspace |
| `:undo` | Restore files to the previous checkpoint |
| `:cost` | Show session token / cost totals |
| `:resume <id>` | Switch to another session (history is replayed) |
| `:q` | Quit (same as `exit`) |

### Resuming a previous session

Sessions are persisted automatically. You can pick up right where you left off:

```bash
# List recent sessions
krodo resume --list

# Resume by session ID (or unique prefix)
krodo resume a3f2b1

# Resume in a specific workspace
krodo resume --root /tmp/krodo-sandbox a3f2b1
```

`krodo resume` replays the stored conversation history into a fresh REPL, so the model
remembers everything from the prior session — files edited, tools called, and dialogue.

### Stable release (v0.1 — not yet on PyPI)

The PyPI upload is **deferred past v0.1.0** while the `krodo` distribution
name finalises. First releases are GitHub Release + `uv tool install
git+https://github.com/wadekun/krodo`. PyPI upload will land in a minor
release after the name is locked; see [`CHANGELOG.md`](CHANGELOG.md) for
status.

Install from GitHub in the meantime:

```bash
uv tool install git+https://github.com/wadekun/krodo
krodo --help
```

## Available tools (11 total)

| Tool | Category | Requires approval | Description |
|:-----|:---------|:-----------------:|:------------|
| `read_file` | Read | No | Read a file (with optional offset/limit) |
| `list_dir` | Read | No | List directory contents (depth-limited, noise-dirs skipped) |
| `glob` | Read | No | Find files matching a pattern (`**/*.py`) |
| `grep` | Read | No | Regex search; ripgrep when available, Python fallback |
| `git_status` | Git | No | Show working tree status (`git status --porcelain`) |
| `git_diff` | Git | No | Show unified diff (staged/unstaged, optional path filter) |
| `write_file` | Write | Yes | Write or overwrite a file |
| `edit_file` | Write | Yes | Targeted string replacement with uniqueness enforcement |
| `apply_patch` | Write | Yes | Apply a unified diff atomically with rollback on failure |
| `run_shell` | Shell | Yes | Execute a shell command inside the workspace sandbox |
| `git_commit` | Git | Yes | Commit staged files (API keys auto-redacted from message) |

## Context & Recovery

Krodo enforces a token budget and offers dual compression strategies so that long sessions never overflow the model's context window.

### Token budget (§3.4.1)

The budget is 80% of the model's context window.  At 80% usage, compression is triggered.  At 95%, hard truncation kicks in as a safety net.  If the available budget hits zero, the next turn is refused with a clear message.

```
Total budget   = model_context_window × 0.80
Output reserve = total_budget × 0.15
Compress at    = 80% of budget (default)
Truncate at    = 95% of budget
```

### Compression strategies

Select via `KRODO_COMPRESS` environment variable:

| Strategy | Env value | Description |
|:---------|:----------|:------------|
| LLM summary (default) | `llm` | Calls the same LLM provider to summarise the oldest N dialogue rounds into a `<SUMMARY>…</SUMMARY>` block. |
| Algorithmic | `algorithmic` | Drops `tool_result` content, keeps tool-call metadata and file paths. Zero extra LLM cost — great for offline dev or large codebases. |

```bash
# Use algorithmic compression (no extra LLM calls):
KRODO_COMPRESS=algorithmic uv run krodo "..."

# Override the token ratio for Claude (default 1.1x, tiktoken undercounts):
KRODO_TOKEN_RATIO=1.15 uv run krodo --model anthropic/claude-3-5-sonnet "..."
```

### Error recovery (7 scenarios, §7.5)

| # | Scenario | Recovery |
|---|----------|----------|
| 1 | LLM returns invalid tool-call JSON | Re-inject schema + error; retry ×2, then abort |
| 2 | Tool execution timeout | Kill subprocess; skip tool call with truncated partial result |
| 3 | Agent stall (3× same write-tool call) | Abort turn; show last 3 calls to user |
| 4 | Compression-induced context loss | Re-inject pinned file paths + last user message |
| 5 | File externally modified (SHA-256 conflict) | Block write; ask agent to re-read the file first |
| 6 | Provider rate limit / 5xx | Exponential back-off ×3 (1 s / 2 s / 4 s) |
| 7 | File permission denied (EACCES) | Skip write; report path + permission bits |

### CLI flags

```bash
# Limit tool calls per turn (default 25):
uv run krodo --max-tool-calls 5 "..."

# Set compression window (how many dialogue rounds to compress at once):
uv run krodo --summary-window 3 "..."
```

## Persistence & Memory

### Session storage

Every session is automatically saved to `.krodo/sessions/<session_id>.jsonl` in your workspace. Each line is a JSON event (`USER_MESSAGE`, `ASSISTANT_MESSAGE`, `TOOL_CALL`, `TOOL_RESULT`, `COMPRESSION`, etc.) with a monotonic `seq` number so multi-process appends are safe.

Application logs go to `.krodo/logs/<session_id>.log` (pure `structlog` JSONL — separate from session events).

### AGENTS.md — project memory

Place an `AGENTS.md` file anywhere in your project and Krodo will inject it automatically into every session as `<project_memory>`:

| Tier | Location | Purpose |
|------|----------|---------|
| System | `~/.config/krodo/AGENTS.md` | Personal conventions (applies to all workspaces) |
| Project | `<workspace>/AGENTS.md` | Project-specific rules (always included, never dropped) |
| Subdir | `<cwd>/AGENTS.md` … up to workspace root | Contextual docs for the directory you're working in |

Each file is limited to 8K tokens; total budget is 12K tokens (subdirectory files are dropped first if the limit is hit).

### Configuration files

Defaults can be set in `.krodo/config.yaml` (workspace) or `~/.config/krodo/config.toml` (user-global). Precedence: CLI flag > env var > workspace > user > built-in default.

Quick example:

```yaml
# .krodo/config.yaml — workspace-level default
model: deepseek/deepseek-v4-flash
approval: auto_edit
max_tool_calls: 15
```

**Full field reference + 10 providers + per-provider API keys + troubleshooting
(field-name gotchas, proxy caveats, error-pattern diagnosis): see
[Models & Providers](docs/MODELS.md).**

Run `krodo doctor` after every config change to verify what's actually loaded.

## .krodoignore & Git checkpoint

Two safety nets: a 4-tier ignore system and automatic git checkpointing before every write.

### .krodoignore — 4-tier path filtering (§5.3)

Every `read_file`, `list_dir`, `glob`, and `grep` call passes through `KrodoIgnore` before touching the disk.  Rules are merged from four sources in increasing specificity order:

| Tier | Source | Overridable? |
|------|--------|-------------|
| 1 | Hard-coded defaults (`.env`, `*.pem`, `id_rsa`, `node_modules/`, etc.) | ❌ always active |
| 2 | Project `.gitignore` | — |
| 3 | Project `.krodoignore` (workspace root) | adds custom patterns |
| 4 | User-level `~/.config/krodo/krodoignore` | personal overrides |

When a path matches any rule, the tool returns:
```
PathIgnoredError: '<path>' is ignored (rule: '<pattern>' from <source>)
```

#### Example `.krodoignore`

```gitignore
# Exclude internal data directories from agent reads
data/raw/
reports/*.csv

# Exclude generated mock files
tests/fixtures/generated/
```

### Git checkpoint (§5.4)

Before every write (`write_file`, `edit_file`, `apply_patch`) and write-heuristic shell command, Krodo creates a lightweight `git stash create` checkpoint:

1. Collect affected paths.
2. `checkpoint_sha = git stash create` — does **not** push to the stash stack; working tree is untouched.
3. Emit a `CHECKPOINT` `SessionEvent` to `.krodo/logs/<session>.jsonl`.
4. Execute the write.

On non-git workspaces, checkpointing degrades to a no-op (warning logged; writes proceed normally).

### krodo undo

```bash
# Undo the last checkpoint in the most recent session:
krodo undo [--root <workspace>]

# Undo a specific session:
krodo undo --session <session_id> [--root <workspace>]
```

`krodo undo` reads the session JSONL, finds the most recent `CHECKPOINT` event, and runs `git checkout <sha> -- <affected_paths>` to restore only those paths.  Other files are untouched.

| Condition | Behaviour |
|-----------|-----------|
| Non-git workspace | Exit 1 with friendly error |
| No CHECKPOINT found | Exit 1 with log path hint |
| `affected_paths` = workspace root (shell command scope) | Prompts for confirmation before restoring |

## CLI subcommand semantics

Krodo has three named subcommands — `resume`, `undo`, and `doctor` — alongside a free-form headless prompt. The parser resolves the two as follows:

| Invocation | Behaviour |
|---|---|
| `krodo "create a mario game"` | Headless — prompt is `"create a mario game"` |
| `krodo` | Interactive REPL |
| `krodo resume` | Resume subcommand (most recent session) |
| `krodo resume abc123` | Resume subcommand with session ID `abc123` |
| `krodo resume --root /path` | Resume subcommand; `--root` goes to `resume` |
| `krodo --root /path resume` | Resume subcommand; global `--root` inherited as default |
| `krodo undo` | Undo subcommand |
| `krodo doctor` | Doctor subcommand |

**Key rules:**

- The first **non-option** token is checked against registered subcommand names. If it matches, the token triggers subcommand dispatch — it is never treated as the headless prompt.
- Global flags (`--root`, `--model`, `--approval`, etc.) can go **before or after** the subcommand token. When placed before, they are propagated to the subcommand as defaults; an explicit flag in the subcommand itself always wins.
- Natural-language prompts should be **quoted** so they arrive as a single token. Without quotes, the first word could match a subcommand name:
  ```bash
  krodo "resume the work from yesterday"   # ✓ headless with full prompt
  krodo resume the work from yesterday     # ✗ routes to resume subcommand; "the" is unexpected arg
  ```

## Local development

Krodo uses [`uv`](https://docs.astral.sh/uv/) for dependency and venv management.

```bash
git clone https://github.com/wadekun/krodo
cd krodo
uv sync                       # install deps + create .venv
uv run pytest                 # run tests
uv run ruff check             # lint
uv run mypy src               # type-check
```

Set your LLM credentials in the environment (any subset, depending on the provider you use):

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...
# or use the system keyring (recommended for shared machines, see docs/architecture.md §7.2)
```

## Project layout

```
krodo/
  src/krodo/
    cli/        # Typer entry, REPL, headless exec
    core/       # Agent loop, Context, Budget, Compression, Recovery, Events
    llm/        # LLMProvider Protocol + LiteLLM adapter
    tools/      # File / shell / patch / search / git tools
    sandbox/    # Path firewall, command policy, approval modes
    memory/     # JSONL session store, krodo resume, AGENTS.md loader, config
    obs/        # structlog + OpenTelemetry + cost tracker
  tests/{unit,integration,e2e}/
  docs/
    architecture.md         # design baseline (read this first)
    reviews/                # past architecture review notes
  scripts/
    prototype.py            # Phase 0 single-file prototype (DEPRECATED — use krodo CLI)
```

## Contributing

This is a learning + production project. Contributions welcome — Phase 1 is feature-complete and the CI gate is stable.

Ground rules:

- All code goes through `ruff` + `mypy --strict` + `pytest --cov` (see [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full CI gate).
- All new tools come with 100% unit test coverage and an integration test against a recorded LLM response (`vcrpy`).
- All changes that touch the agent loop must pass the regression matrix (Phase 2+).
- See [`docs/architecture.md`](docs/architecture.md) §11 for the seven engineering principles.

## Documentation

| Document | What's in it |
|----------|-------------|
| [`docs/QUICKSTART.md`](docs/QUICKSTART.md) | 5-minute install + first task |
| [`docs/MODELS.md`](docs/MODELS.md) | Model & provider config, switching, troubleshooting |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Dev setup, CI gate, PR flow, commit conventions |
| [`SECURITY.md`](SECURITY.md) | Threat model, sandbox boundaries, vulnerability reporting |
| [`CHANGELOG.md`](CHANGELOG.md) | Milestone-by-milestone changes |
| [`docs/architecture.md`](docs/architecture.md) | Full design baseline (the source of truth) |
| [`AGENTS.md`](AGENTS.md) | Auto-loaded project memory (loaded into every session) |

## License

[Apache-2.0](LICENSE) © The Krodo Contributors
