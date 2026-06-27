# Quickstart

> 5-minute guide: install Krodo, configure an API key, run your first task.

## 1. Install

Krodo is **pre-alpha (v0.1.0)**. The PyPI upload is deferred while the
`krodo` distribution name is being finalised; install from source for now.

### Prerequisites

- Python **3.12+** (`python --version`)
- [`uv`](https://docs.astral.sh/uv/) — fast Python package manager
- Git (for checkpoints; Krodo degrades gracefully without it)
- Optional: [`ripgrep`](https://github.com/BurntSushi/ripgrep) for fast `grep`

### From source (recommended for v0.1.0)

```bash
git clone https://github.com/liangck/krodo
cd krodo
uv sync
```

That's it. Verify it runs:

```bash
uv run krodo --help
```

### As a library / CLI tool (Phase 2)

Once PyPI is live:

```bash
uv tool install krodo
# or
pipx install krodo
```

## 2. Configure an API key

Krodo uses [LiteLLM](https://github.com/BerriAI/litellm) under the hood,
so any LiteLLM-supported provider works. Set one environment variable:

```bash
export ANTHROPIC_API_KEY=sk-ant-...      # Claude
# or
export OPENAI_API_KEY=sk-...             # GPT
# or
export GEMINI_API_KEY=...                # Gemini
# or
export KRODO_API_KEY=...                 # provider-agnostic fallback
```

To point at a custom endpoint (Azure OpenAI, LiteLLM Proxy, Ollama,
vLLM, etc.):

```bash
export KRODO_API_BASE=http://localhost:11434
```

To switch models:

```bash
export KRODO_MODEL=anthropic/claude-3-5-sonnet-20241022   # default
export KRODO_MODEL=openai/gpt-4o
export KRODO_MODEL=gemini/gemini-1.5-pro
export KRODO_MODEL=ollama/llama3
```

Verify with `krodo doctor`:

```bash
uv run krodo doctor
```

## 3. Pick an approval mode

Krodo refuses to do anything unsafe unless you let it. Three modes:

| Mode | Reads | Writes | Shell exec | When to use |
|------|------|-------|-----------|-------------|
| `read_only` | ✅ | ❌ | ❌ | "Look but don't touch" — exploration, Q&A |
| `auto_edit` *(default)* | ✅ | confirm y/n | confirm y/n | Daily coding — you stay in control |
| `full_auto` | ✅ | ✅ | ✅ | Trusted, repeated tasks (⚠️ red banner) |

Set via `--approval` or `KRODO_APPROVAL`:

```bash
uv run krodo --approval read_only "explain what main.py does"
```

In `auto_edit` you can answer `a` (approve all future calls from this
tool) or `p` (enter a pattern rule like `run_shell pytest*`) to reduce
prompts.

## 4. Three entry shapes

### Headless — one task, then exit

```bash
uv run krodo --root /path/to/project "add a docstring to src/main.py"
```

### REPL — interactive multi-turn dialogue

```bash
uv run krodo --root /path/to/project
you> read package.json and tell me the entry point
... (krodo answers) ...
you> now rename it to app.js and update the package.json
... (krodo edits, you approve) ...
you> exit          # or Ctrl-D / two Ctrl-C / :q
```

Conversation history persists across turns, so "now do X" or "fix the
bug from before" work naturally.

### Pipe — stdin as prompt or context

```bash
# stdin IS the prompt
echo "explain this function" | uv run krodo --root /path/to/project

# stdin is appended as context when a prompt is given
git diff | uv run krodo "review this change for bugs"
```

## 5. Slash commands (REPL only)

Handled locally — never sent to the LLM:

| Command | Action |
|:--------|:-------|
| `:help` | List commands |
| `:sessions` | Show the 10 most recent sessions in this workspace |
| `:undo` | Restore files to the previous checkpoint |
| `:cost` | Show session token / cost totals |
| `:resume <id>` | Switch to another session (history is replayed) |
| `:q` | Quit |

## 6. Resume a previous session

Sessions are persisted automatically under `<workspace>/.krodo/sessions/`.

```bash
# List recent sessions
uv run krodo resume --list

# Resume by session ID (or unique prefix)
uv run krodo resume a3f2b1

# Resume in a specific workspace
uv run krodo resume --root /path/to/project a3f2b1
```

`krodo resume` replays the stored event log into a fresh REPL, so the
model remembers everything from the prior session — files edited, tools
called, dialogue. Approval trust (`a` / `p` answers from the previous
session) is restored too.

## 7. Undo the last write

Every write tool call snapshots the affected paths via `git stash create`.
`krodo undo` restores them:

```bash
uv run krodo undo                          # most recent session, this workspace
uv run krodo undo --session a3f2b1         # specific session
uv run krodo undo --root /path/to/project  # specific workspace
```

Requires the workspace to be a git repo. Non-git workspaces get a
`checkpoint_skipped` warning (once per session) instead of a hard error.

## 8. Read the docs

- [`README.md`](../README.md) — project overview, roadmap
- [`docs/architecture.md`](architecture.md) — full design baseline
- [`AGENTS.md`](../AGENTS.md) — auto-loaded project memory (contributor guide)
- [`CONTRIBUTING.md`](../CONTRIBUTING.md) — dev setup, CI gate, PR flow
- [`SECURITY.md`](../SECURITY.md) — supported versions, sandbox model
- [`CHANGELOG.md`](../CHANGELOG.md) — milestone-by-milestone changes

## 9. Troubleshooting

| Symptom | Fix |
|---------|-----|
| `command not found: krodo` | Use `uv run krodo ...` from the project root, or `uv tool install .` |
| `AuthenticationError` from LLM | API key env var missing or wrong — try `krodo doctor` |
| `checkpoint_skipped` warning | Workspace isn't a git repo; either `git init` or accept that `krodo undo` won't work |
| Approval prompts too chatty | In `auto_edit`, press `a` to trust this tool for the session, or `p` to add a pattern rule |
| Output truncated mid-tool-call | Hit the per-turn tool-call limit; REPL offers to continue. Or pass `--max-tool-calls 50` |
| Context window overflow | `krodo` auto-compresses at 80%; set `KRODO_COMPRESS=algorithmic` to drop tool-result content for zero-LLM-cost compression |

For more, see [`docs/architecture.md`](architecture.md) §11 (engineering
rules) and §12 ( FAQs).
