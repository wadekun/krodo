# Models & Providers

Krodo talks to LLMs through [LiteLLM](https://github.com/BerriAI/litellm), so any
LiteLLM-supported provider works out of the box. This doc covers how to configure
the model, switch providers, and diagnose common issues.

If you only need to get started quickly, the [Quickstart](QUICKSTART.md) is shorter.

## Quick reference

| Provider | Model example | API key env var | Where to get a key |
|----------|--------------|-----------------|--------------------|
| Anthropic | `anthropic/claude-sonnet-4-5-20250929` | `ANTHROPIC_API_KEY` | https://console.anthropic.com |
| OpenAI | `openai/gpt-4o` | `OPENAI_API_KEY` | https://platform.openai.com |
| Z.AI (GLM) | `zai/glm-4.6` | `ZAI_API_KEY` | https://z.ai |
| DeepSeek | `deepseek/deepseek-v4-flash` | `DEEPSEEK_API_KEY` | https://platform.deepseek.com |
| Google Gemini | `gemini/gemini-2.5-pro` | `GEMINI_API_KEY` | https://ai.google.dev |
| Mistral | `mistral/mistral-large-latest` | `MISTRAL_API_KEY` | https://console.mistral.ai |
| Cohere | `cohere/command-r-plus` | `COHERE_API_KEY` | https://cohere.com |
| Ollama (local) | `ollama/llama3` | (none) | run `ollama serve` |
| vLLM (local) | `openai/<model>` + `api_base` | (per deployment) | your server |
| OpenAI-compatible (any) | `openai/<model>` + `api_base` | (per deployment) | your proxy |

**Picking a model**: Claude Sonnet and GPT-4o have the best tool-calling accuracy
for coding agents. DeepSeek V4 Flash is the cheapest option that's still solid for
code. GLM-4.6 is strong on Chinese-language tasks. Local Ollama is free but quality
depends on your hardware.

## Four ways to set the model (highest → lowest priority)

### 1. CLI flag — one-shot

```bash
uv run krodo --model deepseek/deepseek-v4-flash "task"
```

Most explicit; overrides everything else. Best for one-time model swaps or testing.

### 2. Environment variable — current shell

```bash
export KRODO_MODEL=deepseek/deepseek-v4-flash
uv run krodo "task 1"
uv run krodo "task 2"
```

Persists for the current shell session. Useful when benchmarking across runs.

### 3. Workspace config — per-project default

File: `<workspace>/.krodo/config.yaml` (YAML format).

```yaml
model: deepseek/deepseek-v4-flash
approval: auto_edit
max_tool_calls: 15
```

Commits to git, so the whole team shares the default. Override per-invocation with
CLI flag or env var when needed.

### 4. User config — global default

File: `~/.config/krodo/config.toml` (TOML format — note the file extension differs
from the workspace config).

```toml
model = "deepseek/deepseek-v4-flash"
```

Applies to every project that doesn't have its own workspace config.

### Precedence

```
CLI flag  >  KRODO_MODEL env  >  .krodo/config.yaml  >  ~/.config/krodo/config.toml  >  built-in default
```

Each field is resolved independently. If `model` is set in two sources, the
higher-priority one wins, but other fields (e.g. `max_tokens`) still flow through
from lower-priority sources if not overridden.

## Config field reference

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `model` | `str` | `anthropic/claude-3-5-sonnet-20241022` | LiteLLM model string (e.g. `zai/glm-4.6`) |
| `api_base` | `str` | (provider default) | Custom endpoint URL (for proxies / self-hosted) |
| `api_key` | `str` | (from provider env var) | Override the auto-discovered key |
| `approval` | `"read_only"` / `"auto_edit"` / `"full_auto"` | `auto_edit` | Write/exec approval mode |
| `max_tokens` | `int` | `16384` | Output token budget per LLM response |
| `max_tool_calls` | `int` | `25` | Tool-call limit per turn |
| `summary_window` | `int` | `2` | Dialogue rounds compressed per pass |
| `compress` | `"llm"` / `"algorithmic"` | `llm` | Context compression strategy |
| `prompt_cache` | `bool` | `true` | Anthropic prompt caching for the system message (see [Prompt caching](#prompt-caching)) |

### ⚠️ Field-name gotcha

Pydantic (our config validator) silently ignores unknown fields. These are all
**discarded without warning**:

```yaml
# ❌ Wrong field names — silently ignored, value falls back to default
provider: deepseek/deepseek-v4-flash         # use `model:` instead
approval_mode: auto_edit                     # use `approval:` instead
max_tool_calls_per_turn: 15                  # use `max_tool_calls:` instead
soft_warning_at: 10                          # not a config field at all
```

The result is that the *displayed* config (via `krodo doctor`) shows your values,
but the *actually used* values fall back to defaults. If your config doesn't seem
to apply, run `krodo doctor` and check both the "config sources" block (what your
file says) AND the actual config summary (what the LLM call will use).

## Setting API keys

Each provider reads its own env var (see Quick reference above). For multi-provider
workflows, set them all in your shell profile:

```bash
# ~/.zshrc or ~/.bashrc
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...
export ZAI_API_KEY=...
export DEEPSEEK_API_KEY=sk-...
export GEMINI_API_KEY=...
```

After this, switching providers is just changing the model string — keys are
auto-selected by LiteLLM based on the provider prefix.

### `KRODO_API_KEY` fallback

If you set `KRODO_API_KEY`, it's used as a last-resort for any provider that
doesn't have its own env var set. Useful for proxy setups that route to a single
backend.

## Verify with `krodo doctor`

After changing config, always run:

```bash
uv run krodo doctor
```

Expected output (for a working ZAI setup):

```
krodo doctor — LLM connectivity check

config sources
        /path/to/.krodo/config.yaml
  model     zai/glm-4.6
  approval  auto_edit

model     zai/glm-4.6
api_base  (provider default)
api_key   (from env)

output budget
max_tokens (output)  16,384
context window       126,000 tokens (model default)

Sending 1-token ping…
✓  ping OK  (748 ms)
```

**Critical**: `model` must appear **twice** — once under "config sources" (what
your file says) and once under the actual config summary (what the ping will use).
If they differ, your config didn't apply. Common cause: wrong field name (see
[Field-name gotcha](#field-name-gotcha)).

## Common pitfalls

### "Missing Anthropic API Key" but you set `ZAI_API_KEY`

Cause: your config has a wrong field name (likely `provider:` instead of `model:`).
Pydantic dropped it, `model` stayed `None`, and krodo fell back to the hardcoded
`anthropic/claude-3-5-sonnet-20241022` default.

Fix: rename `provider:` → `model:` in `.krodo/config.yaml`. Run `krodo doctor` to
confirm the resolved model is what you expect.

### "PydanticSerializationUnexpectedValue ... ServerToolUse"

Cause: you're using a provider through an Anthropic-compatible proxy (e.g. GLM via
an Anthropic-shaped wrapper). LiteLLM parses the response with Anthropic's Pydantic
models, which don't fully recognise GLM's content blocks.

Fix: use the provider's native LiteLLM prefix (`zai/` for Z.AI, not `anthropic/`
through a proxy). Native prefixes use the correct response models.

### "Insufficient balance" / `RateLimitError`

Cause: provider account has no credit. LiteLLM reports quota errors as
`RateLimitError` (the same category as rate-limit-per-second).

Fix: log into your provider's console and recharge.

Note: krodo's recovery layer retries `RateLimitError` 3 times with 1s/2s/4s backoff.
For billing errors this is wasted time — the balance won't change in 7 seconds.
Phase 3 will add message-pattern matching to abort immediately on "Insufficient
balance" / "quota exceeded".

### `krodo doctor` shows config but ping uses wrong model

Fixed in commit `68035db`. Before that, doctor displayed the config-supplied model
under "config sources" but the actual ping used the hardcoded default. If you're
on a version with this fix (v0.1.0 post-`68035db`), doctor honours the config
model. Update if you're on an older version.

### Switching models keeps hitting the old provider

Cause: a leftover env var (`KRODO_MODEL` or a provider-specific key) is overriding
config.

Fix:

```bash
unset KRODO_MODEL
env | grep -E "KRODO_|ANTHROPIC_|OPENAI_|ZAI_|DEEPSEEK_|GEMINI_"  # check for leftovers
```

Then retry.

### Config works for `krodo "task"` but not `krodo resume`

The resume subcommand has its own `_DEFAULT_MODEL` constant. If config-based
override isn't applied (older bug, similar to doctor), update to a version with
the fix.

## Prompt caching

For Anthropic models (`anthropic/*`), krodo tags the system message with
`cache_control: {"type": "ephemeral"}` so the static prompt prefix (system
rules + tool list) is cached for ~5 minutes. On multi-turn conversations
this skips re-processing the system prompt every turn, lowering latency
and (for metered APIs) cost. Enabled by default.

OpenAI and Gemini handle prompt caching provider-side and need no
client-side flag, so `prompt_cache` only affects Anthropic models.

### Turning it off

Rarely needed, but if a session is so short that the cache-write cost
outweighs the benefit (cache writes cost ~25% more than a normal read on
Anthropic), disable it:

```yaml
# .krodo/config.yaml
prompt_cache: false
```

## Advanced

### Custom OpenAI-compatible endpoint

For Azure OpenAI, SiliconFlow, Together, Anyscale, LiteLLM Proxy, or your own
vLLM deployment:

```yaml
# .krodo/config.yaml
model: openai/your-model-name
api_base: https://your-endpoint.com/v1
api_key: your-proxy-key  # or set KRODO_API_KEY env var
```

### Ollama (local model, free)

```bash
# Install Ollama from https://ollama.com and start it
ollama serve
ollama pull llama3

# Use it
uv run krodo --model ollama/llama3 --root /tmp/krodo-sandbox "task"
```

Note: Ollama quality for tool-calling is generally lower than cloud models.
Coding-agent experience may degrade, especially for multi-step refactors.

### Switching providers mid-session (REPL)

Inside the REPL, you can't switch models mid-session — the AgentLoop holds a single
provider for its lifetime. Options:

- Exit and restart with a different `--model` flag.
- Use `:resume <id>` to switch session context (still same model).
- Use `krodo resume <id>` from the shell with a different `--model` flag —
  replayed history will be sent to the new model.

### Prompt caching (Anthropic / OpenAI)

Currently krodo does not enable prompt caching explicitly. Phase 2 will add this
(cuts long-session cost ~30%). Track progress via the v0.2 milestone.

### What LiteLLM version do we pin?

`pyproject.toml` pins `litellm>=1.40,<2`. Minor upgrades within that range are
allowed but require a CI green run before merge (engineering rule #7). If you hit
a LiteLLM bug fixed in a newer version, open a PR with the version bump and the
issue link.
