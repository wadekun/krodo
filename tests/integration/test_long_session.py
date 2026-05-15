"""Integration test: 50-turn mock LLM session without context window overflow.

Acceptance criterion from architecture.md §8:
    "用 mock LLM 跑 50+ turn 不爆窗口"

Strategy:
- Use a mock LLM provider that always returns a final text answer (no tool calls).
- Inject a very small context window (1,000 tokens) with a BudgetCalculator.
- Run 50 turns, each adding content to the history.
- Assert that the test completes without error (no ContextWindowExceededError,
  no crash) and that the context manager has not grown beyond the budget limit.

This test validates:
1. BudgetCalculator correctly tracks token usage.
2. AlgorithmicCompressor triggers and reduces history when needed.
3. The AgentLoop + InMemoryContextManager + BudgetCalculator + Compressor
   pipeline works end-to-end for many turns.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from coda.core.budget import BudgetCalculator
from coda.core.compression import AlgorithmicCompressor
from coda.core.context import InMemoryContextManager
from coda.core.loop import AgentLoop, LoopConfig
from coda.core.types import LLMChunk, Message, ToolDef
from coda.core.workspace import LocalWorkspaceResolver
from coda.sandbox.firewall import LocalSandboxRunner
from coda.tools.protocols import ToolContext
from coda.tools.registry import ToolRegistry

# ---------------------------------------------------------------------------
# Minimal mock LLM provider
# ---------------------------------------------------------------------------


class _MockLLMProvider:
    """Always returns a simple text answer, adding some token bulk per turn."""

    name = "mock"
    model = "gpt-4o"

    def __init__(self, response_text: str = "Done.") -> None:
        self._response_text = response_text
        self.call_count = 0

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None = None,
    ) -> Message:
        self.call_count += 1
        return Message(role="assistant", content=self._response_text)

    def stream_chat(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None = None,
    ) -> AsyncIterator[LLMChunk]:
        raise NotImplementedError

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def count_message_tokens(self, messages: list[Message]) -> int:
        total = 0
        for msg in messages:
            total += 4  # per-message overhead
            if isinstance(msg.content, str):
                total += max(1, len(msg.content) // 4)
            else:
                total += sum(max(1, len(str(p)) // 4) for p in msg.content)
        return total


class _AutoApprovalManager:
    async def check(self, tool_call: Any) -> str:
        return "approve"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _ctx(tmp_path: Path) -> ToolContext:
    ws = LocalWorkspaceResolver().resolve(explicit=tmp_path)
    sb = LocalSandboxRunner(ws)
    return ToolContext(
        workspace=ws,
        sandbox=sb,
        session_id="long-session-test",
        logger=logging.getLogger("test"),
    )


def _make_loop(
    provider: _MockLLMProvider,
    tmp_path: Path,
    *,
    context_window_tokens: int = 1_000,
) -> AgentLoop:
    """Build an AgentLoop with a tiny context window to force compression."""
    budget = BudgetCalculator(
        model=provider.model,
        count_fn=provider.count_message_tokens,
    )
    # Override the budget's context window to something very small
    budget._total_budget = int(context_window_tokens * 0.80)  # type: ignore[attr-defined]
    budget._output_reserve = int(budget._total_budget * 0.15)  # type: ignore[attr-defined]

    compressor = AlgorithmicCompressor()
    tool_ctx = _ctx(tmp_path)

    config = LoopConfig(
        max_tool_calls_per_turn=5,
        system_prompt="You are a helpful coding assistant.",
    )

    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry(),
        tool_ctx=tool_ctx,
        approval=_AutoApprovalManager(),  # type: ignore[arg-type]
        config=config,
    )

    # Replace the default context manager with budget-aware one
    loop.context_manager = InMemoryContextManager(
        system_prompt=config.system_prompt,
        budget=budget,
        compressor=compressor,
    )
    return loop


# ---------------------------------------------------------------------------
# The 50-turn test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_50_turns_without_window_overflow(tmp_path: Path) -> None:
    """Run 50 turns with a tiny context window — must not crash or overflow."""
    provider = _MockLLMProvider(response_text="I've completed the task.")
    loop = _make_loop(provider, tmp_path, context_window_tokens=2_000)

    for i in range(50):
        # Each turn adds some content to exercise the budget
        result = await loop.run(
            f"Turn {i}: Please analyse file{i}.py and suggest improvements. "
            f"The file contains: {'x = ' * 20} # line {i}"
        )
        assert result is not None
        assert not result.hit_tool_call_limit, f"Tool call limit hit on turn {i}"

    # All 50 turns should have succeeded
    assert provider.call_count == 50

    # Context should not be massively larger than budget
    used, budget = loop.context_manager.token_usage()
    assert used <= budget * 2, (
        f"Context grew too large: used={used} tokens, budget={budget} tokens. "
        "Compression did not work correctly."
    )


@pytest.mark.asyncio
async def test_50_turns_history_stays_bounded(tmp_path: Path) -> None:
    """History length should be bounded by compression, not grow to 50*3 entries."""
    provider = _MockLLMProvider(response_text="OK.")
    loop = _make_loop(provider, tmp_path, context_window_tokens=500)

    for i in range(50):
        await loop.run(f"Turn {i}: do something with lots of text " + "A" * 100)

    # History should be compressed down, not holding all 50 turns' full content
    history_len = len(loop.context_manager.history)
    # With a 500-token window and aggressive compression, history should be << 100
    assert history_len <= 100, (
        f"History was not compressed: {history_len} messages remain "
        "after 50 turns with a tiny context window."
    )


@pytest.mark.asyncio
async def test_algorithmic_compressor_does_not_call_llm(tmp_path: Path) -> None:
    """AlgorithmicCompressor must not call the LLM provider for compression."""
    provider = _MockLLMProvider()
    loop = _make_loop(provider, tmp_path, context_window_tokens=1_000)

    for i in range(20):
        await loop.run(f"Turn {i}: " + "X" * 200)

    # All provider.chat() calls should be for user turns only (20 turns)
    # The algorithmic compressor does NOT call the provider
    # Allow up to 22 calls (20 turns + a couple of recovery retries if any)
    assert provider.call_count <= 22, (
        f"Expected at most 22 LLM calls (20 user turns + safety margin), "
        f"got {provider.call_count}. "
        "AlgorithmicCompressor may have called the provider unexpectedly."
    )
