"""Cost tracking — per-session token and USD accumulation (M6.2).

Per AGENTS.md rule #4, every LLM call must record tokens + cost.  The
provider attaches ``usage`` / ``cost_usd`` to each assistant Message;
AgentLoop feeds them into a session-scoped ``CostTracker`` and emits a
``COST_SNAPSHOT`` event per turn.
"""

from __future__ import annotations


class CostTracker:
    """Accumulate prompt/completion tokens and USD cost across a session.

    ``cost_usd`` stays ``None`` until at least one call reports a known cost
    (unknown models yield no pricing) — callers omit the cost display then.
    """

    def __init__(self) -> None:
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._cost_usd: float | None = None

    def add(self, usage: dict[str, int] | None, cost_usd: float | None = None) -> None:
        """Record one LLM call's usage and (optionally) its cost."""
        if usage:
            self._prompt_tokens += int(usage.get("prompt_tokens", 0) or 0)
            self._completion_tokens += int(usage.get("completion_tokens", 0) or 0)
        if cost_usd is not None:
            self._cost_usd = (self._cost_usd or 0.0) + cost_usd

    @property
    def prompt_tokens(self) -> int:
        return self._prompt_tokens

    @property
    def completion_tokens(self) -> int:
        return self._completion_tokens

    @property
    def total_tokens(self) -> int:
        return self._prompt_tokens + self._completion_tokens

    @property
    def cost_usd(self) -> float | None:
        return self._cost_usd


def estimate_cost_usd(model: str, usage: dict[str, int]) -> float | None:
    """Estimate USD cost for *usage* on *model* via LiteLLM's price table.

    Returns None for unknown models — tokens are still tracked, only the
    cost display is omitted.
    """
    try:
        import litellm  # noqa: PLC0415

        prompt_cost, completion_cost = litellm.cost_per_token(
            model=model,
            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage.get("completion_tokens", 0) or 0),
        )
        return float(prompt_cost) + float(completion_cost)
    except Exception:  # noqa: BLE001
        return None


def format_token_count(count: int) -> str:
    """Human-friendly token count: 950 → '950', 12345 → '12.3k'."""
    if count >= 1000:
        return f"{count / 1000:.1f}k"
    return str(count)


__all__ = ["CostTracker", "estimate_cost_usd", "format_token_count"]
