# M8 Code Review — Provider Matrix CI + Prompt Caching

**Branch**: `feat/phase2-m8-ci`
**Diff**: +196 / -3 across 9 tracked files + 1 new untracked file
**Tests**: 708 passed ✅ | ruff ✅ | mypy strict ✅

---

## Verdict: ✅ LGTM — 1 bug fix required, 2 minor suggestions

---

## PR1 — Provider Matrix CI

### [ci.yml](file:///Volumes/lck_mac_ext/source_code/opensource/ai-agents/krodo/.github/workflows/ci.yml) ✅

Clean implementation. Key design decisions correctly landed:

- `needs: lint-type-test` — matrix only runs after primary CI passes ✅
- `continue-on-error: true` + `fail-fast: false` — flaky providers never block merge ✅
- `if: github.event.pull_request.draft == false` — saves API cost on draft PRs ✅
- 3 provider matrix (Ollama deferred) — pragmatic scope ✅
- All 3 API key env vars injected from secrets, no hardcoding ✅

> [!TIP]
> The `uv sync --all-groups --frozen` + `uv python install 3.12` setup is identical to the lint-type-test job. If CI YAML grows much more, consider extracting a composite action, but not worth it at this stage.

### [provider_e2e.py](file:///Volumes/lck_mac_ext/source_code/opensource/ai-agents/krodo/.github/workflows/scripts/provider_e2e.py) ✅

Well-structured smoke script:

- **Graceful skip** when key is unset (exit 0) — critical for landing before secrets are configured ✅
- **Does not assert exact model output** — correctly recognizes LLM non-determinism ✅
- Exit code semantics clear: 0=pass/skip, 1=fail, 2=usage error ✅
- `_DEFAULT_MODELS` uses cheap models per provider (haiku/4o-mini/flash-lite) ✅
- Model override via `<PROVIDER>_MODEL` env var — nice escape hatch ✅

### [pyproject.toml](file:///Volumes/lck_mac_ext/source_code/opensource/ai-agents/krodo/pyproject.toml) ✅

- `provider_matrix` marker registered with descriptive string ✅
- `.github/workflows/scripts/**` per-file-ignores (T201 + S603) — correctly scoped ✅

---

## PR2 — Prompt Caching

### [litellm_provider.py](file:///Volumes/lck_mac_ext/source_code/opensource/ai-agents/krodo/src/krodo/llm/litellm_provider.py) ✅

Core implementation is clean and correct:

```python
if (
    self._prompt_cache
    and self.model.startswith("anthropic/")
    and litellm_messages
    and litellm_messages[0].get("role") == "system"
):
    litellm_messages[0]["cache_control"] = {"type": "ephemeral"}
```

- Guard chain is defensive: checks `_prompt_cache` flag → model prefix → non-empty messages → system role ✅
- Only tags system message (not user/assistant) — correct, static prefix gets the most cache hits ✅
- `litellm_messages` extracted to a variable before the guard (was inline before) — cleaner ✅
- `_build_kwargs` shared by both `chat()` and `stream_chat()`, so caching applies to both paths ✅

### [config.py](file:///Volumes/lck_mac_ext/source_code/opensource/ai-agents/krodo/src/krodo/core/config.py) ✅

`prompt_cache: bool | None = None` — three-state (`True`/`False`/`None`=inherit default) is the right pattern, consistent with `max_tool_calls`, `summary_window` etc.

### [main.py](file:///Volumes/lck_mac_ext/source_code/opensource/ai-agents/krodo/src/krodo/cli/main.py) ✅

```python
prompt_cache_value = cfg.prompt_cache if cfg.prompt_cache is not None else True
```

Correctly resolves `None` → `True` (default on). Passed to both headless and REPL+slash-resume paths in `main()`.

---

## 🐛 Bug: `resume.py` misses `prompt_cache`

> [!WARNING]
> **`krodo resume <id>` does NOT pass `prompt_cache` to `_build_session_components`**, so it always gets the default `True` — even if the user's config.yaml says `prompt_cache: false`.

In [resume.py L97-107](file:///Volumes/lck_mac_ext/source_code/opensource/ai-agents/krodo/src/krodo/cli/resume.py#L97-L107):

```python
def _rebuild(target_id: str) -> SessionComponents:
    return _build_session_components(
        root=root,
        model=model,
        api_key=api_key,
        api_base=api_base,
        approval_mode=approval,
        max_tool_calls=max_tool_calls,
        max_tokens=max_tokens,
        resume_session_id=target_id,
        # ❌ prompt_cache= is missing
    )
```

Since `_build_session_components` defaults `prompt_cache=True`, this silently ignores the user's config when resuming. The fix is small:

1. `resume_command()` needs to resolve `prompt_cache` from `cfg` (same as `main()` does).
2. Pass it to `_rebuild()` via closure.

**Severity**: Low (only affects users who explicitly set `prompt_cache: false` in config.yaml AND use `krodo resume`). But it's a correctness gap — config values should be honored uniformly across all entry points.

```diff
 # In resume.py, inside resume_command(), add config resolution:
+    prompt_cache_value = cfg.prompt_cache if cfg.prompt_cache is not None else True

 def _rebuild(target_id: str) -> SessionComponents:
     return _build_session_components(
         ...
         max_tokens=max_tokens,
         resume_session_id=target_id,
+        prompt_cache=prompt_cache_value,
     )
```

> [!NOTE]
> You'll also need to check where `cfg` is resolved in `resume_command()`. The `resume` subcommand has its own config loading — confirm that `load_config()` is called before the `_rebuild` closure.

---

## 💡 Minor Suggestions (non-blocking)

### 1. `krodo doctor` could show `prompt_cache` status

`krodo doctor` prints config sources and resolved values but doesn't mention `prompt_cache`. Since it's the only new config field in M8, adding one line to the doctor output would help users verify it's working:

```
prompt_cache : true (default)
```

Not blocking — can land in a follow-up.

### 2. `provider_e2e.py` — consider `BLE001` noqa comment

```python
except Exception as exc:  # noqa: BLE001 — surface any provider error
```

The comment is good, but the `BLE001` rule isn't in the project's ruff `select` list (only `B` is selected, and `BLE` is a separate rule set). This noqa is harmless but dead — ruff won't flag it. Cosmetic only.

---

## Test Coverage ✅

The 4 new tests are well-designed:

| Test | What it verifies |
|------|-----------------|
| `test_prompt_cache_default_on_for_anthropic` | Default `True` → system message gets `cache_control` |
| `test_prompt_cache_disabled_no_tag` | `False` → no tags on any message |
| `test_prompt_cache_skipped_for_non_anthropic` | `openai/*` → no tags |
| `test_prompt_cache_noop_without_system_message` | Edge case: user-only messages → no crash |

All 4 test `_build_kwargs` directly (via `_build_kwargs_with_system` helper), which is the right level — this is a unit of logic in a private method, and testing the public `chat()` would require mocking litellm and wouldn't add value for this specific feature.

---

## Documentation ✅

- [MODELS.md](file:///Volumes/lck_mac_ext/source_code/opensource/ai-agents/krodo/docs/MODELS.md) — config table + dedicated "Prompt caching" section with opt-out example ✅
- [CONTRIBUTING.md](file:///Volumes/lck_mac_ext/source_code/opensource/ai-agents/krodo/CONTRIBUTING.md) — clear explanation of non-blocking CI job + how to enable ✅
- [CHANGELOG.md](file:///Volumes/lck_mac_ext/source_code/opensource/ai-agents/krodo/CHANGELOG.md) — two well-written `Added` entries ✅

---

## Summary

| Area | Status | Notes |
|------|--------|-------|
| CI workflow | ✅ | Clean, well-documented, safe-to-land |
| E2E script | ✅ | Pragmatic, graceful-skip, cheap models |
| Prompt caching logic | ✅ | Correct guard chain, system-message-only |
| Config wiring (main) | ✅ | Properly resolved from config.yaml |
| Config wiring (resume) | 🐛 | Missing `prompt_cache=` passthrough |
| Tests | ✅ | 4 tests cover all branches |
| Docs | ✅ | MODELS.md + CONTRIBUTING + CHANGELOG |

**Action**: Fix the `resume.py` `prompt_cache` passthrough, then commit.
