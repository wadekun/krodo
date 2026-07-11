"""Provider e2e smoke test for CI provider_matrix job (Phase 2 M8).

Each provider is a minimal "can we reach the API + auth OK" check via
litellm.acompletion. Not a full agent e2e — the goal is to catch
provider-integration regressions (auth, model id, response shape) before
merge, not to validate agent behavior.

Usage:
    python .github/workflows/scripts/provider_e2e.py <provider>

Providers:
    anthropic  — needs ANTHROPIC_API_KEY
    openai     — needs OPENAI_API_KEY
    gemini     — needs GEMINI_API_KEY
    deepseek   — needs DEEPSEEK_API_KEY
    zai        — needs ZAI_API_KEY (Z.AI / GLM)

Ollama is intentionally absent here: running it in CI needs a docker
container + model pull, which is heavier than this smoke-test scope.
It will land in a follow-up.

Exit codes:
    0  — pass, OR skipped (key not set → graceful skip, CI stays green)
    1  — fail (API error, auth error, unexpected response)
    2  — usage error
"""

from __future__ import annotations

import asyncio
import os
import sys

import litellm

# Cheap model per provider — override via <PROVIDER>_MODEL env if needed.
# Defaults picked for low cost; pin to whatever the team standardises on.
_DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "anthropic/claude-3-5-haiku-latest",
    "openai": "openai/gpt-4o-mini",
    "gemini": "gemini/gemini-2.0-flash-lite",
    "deepseek": "deepseek/deepseek-v4-flash",
    "zai": "zai/glm-4.6",
}

_KEY_ENV: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "zai": "ZAI_API_KEY",
}

_PROMPT = [{"role": "user", "content": "Reply with exactly one word: OK"}]


async def run_e2e(provider: str) -> int:
    key_env = _KEY_ENV[provider]
    if not os.environ.get(key_env):
        # Graceful skip — no key configured yet. Keeps the job green so
        # provider_matrix can land before all secrets are in place.
        print(f"SKIP: {key_env} not set — skipping {provider} e2e")
        return 0

    model = os.environ.get(f"{provider.upper()}_MODEL", _DEFAULT_MODELS[provider])
    print(f"RUN: {provider} e2e via {model}")

    try:
        response = await litellm.acompletion(model=model, messages=_PROMPT)
    except Exception as exc:  # surface any provider error (auth, network, etc.)
        print(f"FAIL: {provider} API call raised: {exc}")
        return 1

    content = response.choices[0].message.content
    if not content:
        print(f"FAIL: {provider} returned empty content")
        return 1

    # We do not assert the exact word "OK" — model output is non-deterministic
    # and the goal is connectivity, not instruction-following. A non-empty
    # reply means auth + model id + response shape all work.
    print(f"PASS: {provider} responded with: {content!r}")
    return 0


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in _DEFAULT_MODELS:
        providers = "|".join(_DEFAULT_MODELS)
        print(f"Usage: {sys.argv[0]} <{providers}>")
        return 2
    return asyncio.run(run_e2e(sys.argv[1]))


if __name__ == "__main__":
    sys.exit(main())
