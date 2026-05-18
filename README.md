# Coda

> A local-first, multi-provider **coding agent CLI**, built with Python 3.12+.
>
> Status: 🚧 **Pre-alpha (v0.1.0)** — actively in Phase 1 development. Not yet ready for general use.

Coda is an open-source coding agent inspired by Claude Code, Codex CLI, and Aider. It runs locally, talks to your codebase through tools (read / edit / shell / git / grep), and supports any LLM provider via [LiteLLM](https://github.com/BerriAI/litellm) — Anthropic, OpenAI, Gemini, DeepSeek, Qwen, plus local models via Ollama / vLLM.

## Why another coding agent

- **Local-first**: your code never leaves your machine except for LLM API calls.
- **Multi-provider from day 1**: switch between Claude, GPT, Gemini, DeepSeek, Qwen, or local models with a single config flag.
- **Three CLI shapes, one core**: `coda` REPL, `coda exec` headless, `coda tui` (Phase 2) — all share the same agent loop.
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
| 1 M4 | `.codaignore` + git checkpoint + `coda undo` + diff preview | ✅ done |
| 2 | tree-sitter symbol index, repo-map, Textual TUI, MCP client | — |
| 3 | OS-level sandbox, evaluation harness, OpenTelemetry / Langfuse | — |
| 4 | Production-grade: Rust hot paths, single-binary distribution, LiteLLM Proxy | — |

## Quick start

### Try the M3 release (Phase 1 in-progress)

```bash
git clone https://github.com/<org>/coda
cd coda
uv sync

export ANTHROPIC_API_KEY=sk-ant-...   # or OPENAI_API_KEY / CODA_API_KEY

mkdir -p /tmp/coda-sandbox
uv run coda --root /tmp/coda-sandbox "create hello.py that prints Hello Coda, then run it"
```

### Stable release (v0.1 — not yet published)

```bash
pipx install coda
coda --help
```

## Available tools (M2, 11 total)

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

## Context & Recovery (M3)

M3 introduces token-budget enforcement and dual compression strategies so that long sessions never overflow the model's context window.

### Token budget (§3.4.1)

The budget is 80% of the model's context window.  At 80% usage, compression is triggered.  At 95%, hard truncation kicks in as a safety net.  If the available budget hits zero, the next turn is refused with a clear message.

```
Total budget   = model_context_window × 0.80
Output reserve = total_budget × 0.15
Compress at    = 80% of budget (default)
Truncate at    = 95% of budget
```

### Compression strategies

Select via `CODA_COMPRESS` environment variable:

| Strategy | Env value | Description |
|:---------|:----------|:------------|
| LLM summary (default) | `llm` | Calls the same LLM provider to summarise the oldest N dialogue rounds into a `<SUMMARY>…</SUMMARY>` block. |
| Algorithmic | `algorithmic` | Drops `tool_result` content, keeps tool-call metadata and file paths. Zero extra LLM cost — great for offline dev or large codebases. |

```bash
# Use algorithmic compression (no extra LLM calls):
CODA_COMPRESS=algorithmic uv run coda "..."

# Override the token ratio for Claude (default 1.1x, tiktoken undercounts):
CODA_TOKEN_RATIO=1.15 uv run coda --model anthropic/claude-3-5-sonnet "..."
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

### CLI flags (M3 new)

```bash
# Limit tool calls per turn (default 15):
uv run coda --max-tool-calls 5 "..."

# Set compression window (how many dialogue rounds to compress at once):
uv run coda --summary-window 3 "..."
```

## .codaignore & Git checkpoint (M4)

M4 adds two safety nets: a 4-tier ignore system and automatic git checkpointing before every write.

### .codaignore — 4-tier path filtering (§5.3)

Every `read_file`, `list_dir`, `glob`, and `grep` call passes through `CodaIgnore` before touching the disk.  Rules are merged from four sources in increasing specificity order:

| Tier | Source | Overridable? |
|------|--------|-------------|
| 1 | Hard-coded defaults (`.env`, `*.pem`, `id_rsa`, `node_modules/`, etc.) | ❌ always active |
| 2 | Project `.gitignore` | — |
| 3 | Project `.codaignore` (workspace root) | adds custom patterns |
| 4 | User-level `~/.config/coda/codaignore` | personal overrides |

When a path matches any rule, the tool returns:
```
PathIgnoredError: '<path>' is ignored (rule: '<pattern>' from <source>)
```

#### Example `.codaignore`

```gitignore
# Exclude internal data directories from agent reads
data/raw/
reports/*.csv

# Exclude generated mock files
tests/fixtures/generated/
```

### Git checkpoint (§5.4)

Before every write (`write_file`, `edit_file`, `apply_patch`) and write-heuristic shell command, Coda creates a lightweight `git stash create` checkpoint:

1. Collect affected paths.
2. `checkpoint_sha = git stash create` — does **not** push to the stash stack; working tree is untouched.
3. Emit a `CHECKPOINT` `SessionEvent` to `.coda/logs/<session>.jsonl`.
4. Execute the write.

On non-git workspaces, checkpointing degrades to a no-op (warning logged; writes proceed normally).

### coda undo

```bash
# Undo the last checkpoint in the most recent session:
coda undo [--root <workspace>]

# Undo a specific session:
coda undo --session <session_id> [--root <workspace>]
```

`coda undo` reads the session JSONL, finds the most recent `CHECKPOINT` event, and runs `git checkout <sha> -- <affected_paths>` to restore only those paths.  Other files are untouched.

| Condition | Behaviour |
|-----------|-----------|
| Non-git workspace | Exit 1 with friendly error |
| No CHECKPOINT found | Exit 1 with log path hint |
| `affected_paths` = workspace root (shell command scope) | Prompts for confirmation before restoring |

## Local development

Coda uses [`uv`](https://docs.astral.sh/uv/) for dependency and venv management.

```bash
git clone https://github.com/<org>/coda
cd coda
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
coda/
  src/coda/
    cli/        # Typer entry, REPL, headless exec
    core/       # Agent loop, Context, Budget, Compression, Recovery, Events
    llm/        # LLMProvider Protocol + LiteLLM adapter
    tools/      # File / shell / patch / search / git tools
    sandbox/    # Path firewall, command policy, approval modes
    memory/     # SQLite session store + AGENTS.md loader
    obs/        # structlog + OpenTelemetry + cost tracker
  tests/{unit,integration,e2e}/
  docs/
    architecture.md         # design baseline (read this first)
    reviews/                # past architecture review notes
  scripts/
    prototype.py            # Phase 0 single-file prototype (DEPRECATED — use coda CLI)
```

## Contributing

This is a learning + production project. Contributions welcome once Phase 1 stabilizes.

Ground rules:

- All code goes through `ruff` + `mypy --strict` + `pytest --cov` (see `pyproject.toml`).
- All new tools come with 100% unit test coverage and an integration test against a recorded LLM response (`vcrpy`).
- All changes that touch the agent loop must pass the regression matrix (Phase 2+).
- See [`docs/architecture.md`](docs/architecture.md) §11 for the seven engineering principles.

## License

[Apache-2.0](LICENSE) © The Coda Contributors
