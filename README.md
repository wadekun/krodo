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
| 1 | Usable CLI MVP (REPL + headless exec, 11 tools, approval, git safety net) | 🚧 in progress |
| 2 | tree-sitter symbol index, repo-map, Textual TUI, MCP client | — |
| 3 | OS-level sandbox, evaluation harness, OpenTelemetry / Langfuse | — |
| 4 | Production-grade: Rust hot paths, single-binary distribution, LiteLLM Proxy | — |

## Quick start

### Try the M2 Full Tools (Phase 1 in-progress)

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
    core/       # Agent loop, Context, Session, recovery
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
