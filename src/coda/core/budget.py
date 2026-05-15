"""Token budget calculator — architecture.md §3.4.1.

Responsibilities:
- Maintain the MODEL_CONTEXT_WINDOW lookup table (conservative values at 95%
  of advertised window to leave a safety margin for tiktoken approximation).
- Apply a per-model token ratio (Claude 1.1×, others 1.0×) configurable via
  the CODA_TOKEN_RATIO environment variable.
- Compute fixed overhead once per session (system_prompt + tool_schemas).
- Expose a single BudgetStatus dataclass that tells the caller exactly what
  to do: compress, hard-truncate, refuse next turn, or continue normally.

All token counting is done via the injected count_fn (typically
LLMProvider.count_message_tokens) to keep the calculator testable in
isolation without needing a real LLM provider.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from coda.core.types import Message, ToolDef

# ---------------------------------------------------------------------------
# Model → context window table (tokens, conservative 95% of advertised value)
# ---------------------------------------------------------------------------

MODEL_CONTEXT_WINDOW: dict[str, int] = {
    # Anthropic
    "claude-3-5-sonnet-20241022": 190_000,
    "claude-3-5-haiku-20241022": 190_000,
    "claude-3-opus-20240229": 190_000,
    "claude-3-sonnet-20240229": 190_000,
    "claude-3-haiku-20240307": 190_000,
    "claude-opus-4-5": 190_000,
    "claude-sonnet-4-5": 190_000,
    # OpenAI
    "gpt-4o": 126_000,
    "gpt-4o-mini": 126_000,
    "gpt-4-turbo": 126_000,
    "gpt-4": 8_000,
    "gpt-3.5-turbo": 16_000,
    "o1": 190_000,
    "o1-mini": 126_000,
    "o3-mini": 190_000,
    # DeepSeek
    "deepseek-chat": 61_000,
    "deepseek-coder": 61_000,
    # Google
    "gemini-1.5-pro": 1_900_000,
    "gemini-1.5-flash": 950_000,
    "gemini-2.0-flash": 950_000,
    # Qwen
    "qwen-long": 950_000,
    "qwen-plus": 126_000,
    # Local (conservative defaults)
    "llama-3.1-8b": 126_000,
    "llama-3.1-70b": 126_000,
    "mistral-7b": 30_000,
    # Fallback
    "default": 30_000,
}

# Per-model token ratio: tiktoken undercounts relative to the real tokenizer.
# Claude and most non-GPT models tend to produce slightly more tokens than
# tiktoken's gpt-4o encoding predicts.  Configurable via CODA_TOKEN_RATIO.
_MODEL_RATIO: dict[str, float] = {
    "claude": 1.1,  # prefix match
    "gemini": 1.05,
    "default": 1.0,
}


def _get_ratio(model: str) -> float:
    """Return the token ratio for *model*, honouring CODA_TOKEN_RATIO env var."""
    env_ratio = os.environ.get("CODA_TOKEN_RATIO")
    if env_ratio:
        try:
            return float(env_ratio)
        except ValueError:
            pass
    for prefix, ratio in _MODEL_RATIO.items():
        if prefix in model.lower():
            return ratio
    return _MODEL_RATIO["default"]


def get_context_window(model: str) -> int:
    """Return the conservative context window for *model* (in tokens).

    Strips provider prefixes like ``anthropic/``, ``openai/`` before lookup.
    Falls back to the ``"default"`` entry if the model is unknown.
    """
    # Strip provider prefix (e.g. "anthropic/claude-3-5-sonnet-20241022")
    short = model.split("/")[-1] if "/" in model else model
    window = MODEL_CONTEXT_WINDOW.get(short)
    if window is None:
        # Prefix search for aliases like "claude-3-5-sonnet" without date suffix
        for key, val in MODEL_CONTEXT_WINDOW.items():
            if key != "default" and short.startswith(key):
                window = val
                break
    return window or MODEL_CONTEXT_WINDOW["default"]


# ---------------------------------------------------------------------------
# Budget thresholds (§3.4.1)
# ---------------------------------------------------------------------------

_COMPRESS_THRESHOLD = 0.80  # 80 % used → compress
_TRUNCATE_THRESHOLD = 0.95  # 95 % used → hard truncate + warn
_OUTPUT_RESERVE = 0.15  # reserve 15% of total budget for LLM output


class BudgetAction(Enum):
    """What the caller should do before the next LLM call."""

    OK = "ok"
    COMPRESS = "compress"
    TRUNCATE = "truncate"
    REFUSE = "refuse"  # available < 0 even after truncation


@dataclass
class BudgetStatus:
    used_tokens: int
    budget_tokens: int  # 80% of context window
    fixed_overhead: int
    available_tokens: int
    action: BudgetAction

    @property
    def used_fraction(self) -> float:
        return self.used_tokens / max(self.budget_tokens, 1)


# ---------------------------------------------------------------------------
# BudgetCalculator
# ---------------------------------------------------------------------------


class BudgetCalculator:
    """Per-session token budget tracker.

    Parameters
    ----------
    model:
        LiteLLM model string (may include provider prefix).
    count_fn:
        Function that takes a list of Message objects and returns an integer
        token count.  Typically ``LLMProvider.count_message_tokens``.
    """

    def __init__(
        self,
        model: str,
        count_fn: Callable[[list[Message]], int],
    ) -> None:
        self._model = model
        self._count_fn = count_fn
        self._ratio = _get_ratio(model)
        context_window = get_context_window(model)
        # Total budget is 80% of context window (leave 20% for output + safety)
        self._total_budget = int(context_window * _COMPRESS_THRESHOLD)
        # Reserve 15% of total budget for LLM output generation
        self._output_reserve = int(self._total_budget * _OUTPUT_RESERVE)
        self._fixed_overhead: int | None = None  # computed lazily on first check

    def compute_fixed_overhead(
        self,
        system_prompt: str,
        tool_defs: list[ToolDef],
    ) -> int:
        """Compute and cache the fixed token overhead for this session.

        Called once at session start; subsequent calls return cached value.
        """
        if self._fixed_overhead is not None:
            return self._fixed_overhead

        # Estimate system prompt tokens
        sys_msg = Message(role="system", content=system_prompt)
        sys_tokens = self._count_raw([sys_msg])

        # Estimate tool schema tokens (JSON-encode each schema name+description)
        tool_text = " ".join(f"{t.name}: {t.description}" for t in tool_defs)
        tool_tokens = int(len(tool_text) / 4)  # rough char/4 estimate for schemas

        self._fixed_overhead = sys_tokens + tool_tokens
        return self._fixed_overhead

    def check(self, messages: list[Message]) -> BudgetStatus:
        """Return a BudgetStatus for the current *messages* list.

        The caller (ContextManager) passes the full message list *before* the
        next LLM call; this method determines whether compression, truncation,
        or refusal is necessary.
        """
        overhead = self._fixed_overhead or 0
        used = self._count_raw(messages)
        available = self._total_budget - overhead - used - self._output_reserve

        # Determine action based on §3.4.1 thresholds
        compress_limit = int(self._total_budget * (_COMPRESS_THRESHOLD / _COMPRESS_THRESHOLD))
        truncate_limit = int(
            get_context_window(self._model) * _TRUNCATE_THRESHOLD * _COMPRESS_THRESHOLD
        )

        raw_fraction = used / max(self._total_budget, 1)

        if raw_fraction >= _TRUNCATE_THRESHOLD:
            action = BudgetAction.TRUNCATE
        elif raw_fraction >= _COMPRESS_THRESHOLD:
            action = BudgetAction.COMPRESS
        elif available < 0:
            action = BudgetAction.REFUSE
        else:
            action = BudgetAction.OK

        # Silence unused variable warnings
        _ = compress_limit, truncate_limit

        return BudgetStatus(
            used_tokens=used,
            budget_tokens=self._total_budget,
            fixed_overhead=overhead,
            available_tokens=available,
            action=action,
        )

    def _count_raw(self, messages: list[Message]) -> int:
        """Count tokens, applying the model-specific ratio correction."""
        raw = self._count_fn(messages)
        return int(raw * self._ratio)
