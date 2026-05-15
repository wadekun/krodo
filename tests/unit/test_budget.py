"""Unit tests for src/coda/core/budget.py."""

from __future__ import annotations

import os

import pytest

from coda.core.budget import (
    BudgetAction,
    BudgetCalculator,
    BudgetStatus,
    _get_ratio,
    get_context_window,
)
from coda.core.types import Message, ToolDef
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _simple_count(messages: list[Message]) -> int:
    """Deterministic token count: len of all content strings."""
    total = 0
    for m in messages:
        if isinstance(m.content, str):
            total += len(m.content)
        else:
            total += sum(len(str(p)) for p in m.content)
    return total


def _make_calculator(model: str = "gpt-4o", tokens: int = 1000) -> BudgetCalculator:
    """Return a BudgetCalculator whose count_fn always returns *tokens*."""
    return BudgetCalculator(model=model, count_fn=lambda _msgs: tokens)


# ---------------------------------------------------------------------------
# get_context_window
# ---------------------------------------------------------------------------


class TestGetContextWindow:
    def test_known_model(self) -> None:
        assert get_context_window("gpt-4o") == 126_000

    def test_model_with_provider_prefix(self) -> None:
        assert get_context_window("anthropic/claude-3-5-sonnet-20241022") == 190_000

    def test_unknown_model_falls_back_to_default(self) -> None:
        assert get_context_window("some-unknown-model-xyz") == 30_000

    def test_claude_opus(self) -> None:
        assert get_context_window("claude-3-opus-20240229") == 190_000

    def test_prefix_alias(self) -> None:
        # "claude-opus-4-5" is an entry in the table
        assert get_context_window("claude-opus-4-5") > 0


# ---------------------------------------------------------------------------
# _get_ratio
# ---------------------------------------------------------------------------


class TestGetRatio:
    def test_default_ratio_is_one(self) -> None:
        assert _get_ratio("gpt-4o") == 1.0

    def test_claude_ratio(self) -> None:
        assert _get_ratio("claude-3-5-sonnet-20241022") == 1.1

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CODA_TOKEN_RATIO", "1.25")
        assert _get_ratio("gpt-4o") == 1.25

    def test_invalid_env_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CODA_TOKEN_RATIO", "not-a-number")
        assert _get_ratio("gpt-4o") == 1.0


# ---------------------------------------------------------------------------
# BudgetCalculator.check — threshold logic
# ---------------------------------------------------------------------------


class TestBudgetCalculatorCheck:
    def _calc_with_count(self, model: str, count: int) -> BudgetCalculator:
        return BudgetCalculator(model=model, count_fn=lambda _: count)

    def test_ok_when_under_80_percent(self) -> None:
        # budget = 126_000 * 0.80 = 100_800; used = 50_000 < 80% of 100_800
        calc = self._calc_with_count("gpt-4o", 50_000)
        status = calc.check([Message(role="user", content="hi")])
        assert status.action == BudgetAction.OK

    def test_compress_when_above_80_percent(self) -> None:
        # 80% of budget (100_800) → used ≥ 80_640
        calc = self._calc_with_count("gpt-4o", 81_000)
        status = calc.check([Message(role="user", content="hi")])
        assert status.action == BudgetAction.COMPRESS

    def test_truncate_when_above_95_percent(self) -> None:
        # 95% of budget → used ≥ 95_760
        calc = self._calc_with_count("gpt-4o", 98_000)
        status = calc.check([Message(role="user", content="hi")])
        assert status.action == BudgetAction.TRUNCATE

    def test_budget_tokens_equals_80_percent_of_window(self) -> None:
        calc = self._calc_with_count("gpt-4o", 1000)
        status = calc.check([Message(role="user", content="hi")])
        assert status.budget_tokens == int(126_000 * 0.80)

    def test_used_tokens_reflects_count_fn(self) -> None:
        calc = self._calc_with_count("gpt-4o", 12_345)
        status = calc.check([Message(role="user", content="hi")])
        assert status.used_tokens == 12_345

    def test_ratio_applied_for_claude(self) -> None:
        # ratio = 1.1 for claude; raw count = 1000 → used = 1100
        calc = self._calc_with_count("claude-3-5-sonnet-20241022", 1000)
        status = calc.check([Message(role="user", content="hi")])
        assert status.used_tokens == 1100

    def test_used_fraction_property(self) -> None:
        calc = self._calc_with_count("gpt-4o", 50_000)
        status = calc.check([Message(role="user", content="hi")])
        assert 0 < status.used_fraction < 1


# ---------------------------------------------------------------------------
# BudgetCalculator.compute_fixed_overhead
# ---------------------------------------------------------------------------


class TestComputeFixedOverhead:
    def test_returns_non_negative_int(self) -> None:
        class Params(BaseModel):
            x: str

        tool = ToolDef(name="my_tool", description="does stuff", parameters=Params)
        calc = BudgetCalculator(model="gpt-4o", count_fn=_simple_count)
        overhead = calc.compute_fixed_overhead("You are a helpful assistant.", [tool])
        assert overhead >= 0

    def test_caches_value_on_second_call(self) -> None:
        call_count = 0

        def counting_count(msgs: list[Message]) -> int:
            nonlocal call_count
            call_count += 1
            return len(msgs) * 10

        calc = BudgetCalculator(model="gpt-4o", count_fn=counting_count)
        v1 = calc.compute_fixed_overhead("sys", [])
        v2 = calc.compute_fixed_overhead("sys", [])
        assert v1 == v2
        # Second call must NOT invoke count_fn again
        assert call_count == 1

    def test_overhead_affects_available(self) -> None:
        calc = BudgetCalculator(model="gpt-4o", count_fn=lambda _: 1000)
        calc.compute_fixed_overhead("A" * 1000, [])  # sets _fixed_overhead
        # Check that available = budget - overhead - used - output_reserve
        status = calc.check([Message(role="user", content="hi")])
        assert status.fixed_overhead > 0
        assert status.available_tokens < status.budget_tokens


# ---------------------------------------------------------------------------
# BudgetStatus helper properties
# ---------------------------------------------------------------------------


class TestBudgetStatus:
    def test_used_fraction_zero_budget_guard(self) -> None:
        status = BudgetStatus(
            used_tokens=100,
            budget_tokens=0,
            fixed_overhead=0,
            available_tokens=-100,
            action=BudgetAction.REFUSE,
        )
        assert status.used_fraction >= 0  # no ZeroDivisionError


# ---------------------------------------------------------------------------
# Integration: InMemoryContextManager + BudgetCalculator
# ---------------------------------------------------------------------------


class TestContextManagerWithBudget:
    def test_token_usage_returns_real_counts(self) -> None:
        from coda.core.context import InMemoryContextManager

        calc = BudgetCalculator(model="gpt-4o", count_fn=lambda msgs: len(msgs) * 100)
        ctx = InMemoryContextManager(
            system_prompt="sys",
            budget=calc,
        )
        ctx.add_user_input("hello")
        used, budget = ctx.token_usage()
        assert used > 0
        assert budget == int(126_000 * 0.80)

    def test_compress_if_needed_returns_false_without_budget(self) -> None:
        from coda.core.context import InMemoryContextManager

        ctx = InMemoryContextManager(system_prompt="sys")
        ctx.add_user_input("hello")
        assert ctx.compress_if_needed() is False

    def test_compress_if_needed_truncates_when_over_95_percent(self) -> None:
        from coda.core.context import InMemoryContextManager

        # Make count_fn return a very large number so we're always over threshold
        calc = BudgetCalculator(
            model="gpt-4o",
            count_fn=lambda msgs: 100_000 if len(msgs) > 2 else 1,
        )
        ctx = InMemoryContextManager(system_prompt="sys", budget=calc)
        ctx.add_user_input("user msg 1")
        ctx.add_user_input("user msg 2")
        result = ctx.compress_if_needed()
        assert result is True
        # History should be shorter after truncation
        assert len(ctx.history) < 2

    def test_compress_if_needed_returns_false_when_ok(self) -> None:
        from coda.core.context import InMemoryContextManager

        calc = BudgetCalculator(model="gpt-4o", count_fn=lambda _: 100)
        ctx = InMemoryContextManager(system_prompt="sys", budget=calc)
        ctx.add_user_input("hello")
        assert ctx.compress_if_needed() is False

    def test_token_usage_fallback_without_budget(self) -> None:
        from coda.core.context import InMemoryContextManager

        ctx = InMemoryContextManager(system_prompt="sys")
        ctx.add_user_input("hello world")
        used, budget = ctx.token_usage()
        assert used >= 0
        assert budget == 128_000

    def test_token_usage_with_count_fn_only(self) -> None:
        from coda.core.context import InMemoryContextManager

        ctx = InMemoryContextManager(
            system_prompt="sys",
            model_context_window=50_000,
            count_fn=lambda msgs: len(msgs) * 50,
        )
        ctx.add_user_input("hello")
        used, budget = ctx.token_usage()
        assert used == 100  # 2 messages (system + user) × 50
        assert budget == 50_000
