"""InMemoryContextManager — upgraded in M3 to honour token budgets.

Maintains the full message history in memory.  When a BudgetCalculator is
injected (M3+), compress_if_needed() and token_usage() use real token counts
instead of rough char/4 estimates.

Design principle (separation of concerns):
  - add_user_input()    — **write**: appends the user message to _history.
  - build_messages()    — **read**: returns [system] + _history; no side-effects.
  - append_assistant()  — **write**: appends the assistant message to _history.
  - append_tool_result()— **write**: appends a tool-result message to _history.
  - compress_if_needed()— **side-effect**: may mutate _history to reduce tokens.

All write methods go through _history so that build_messages() always produces
a complete, valid conversation that satisfies the user→assistant→tool_result
alternation required by Anthropic / OpenAI.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from coda.core.types import Message, ToolResult

if TYPE_CHECKING:
    from coda.core.budget import BudgetCalculator


class InMemoryContextManager:
    """In-memory context manager with optional token budget enforcement."""

    def __init__(
        self,
        system_prompt: str,
        model_context_window: int = 128_000,
        *,
        budget: BudgetCalculator | None = None,
        count_fn: Callable[[list[Message]], int] | None = None,
    ) -> None:
        self._system_prompt = system_prompt
        self._context_window = model_context_window
        self._history: list[Message] = []
        self._budget = budget
        # count_fn used as a fallback when no budget calculator is injected
        self._count_fn = count_fn

    # ------------------------------------------------------------------
    # ContextManager Protocol implementation
    # ------------------------------------------------------------------

    def add_user_input(self, user_input: str) -> None:
        """Append the user message to history (write, no side-effects on read)."""
        self._history.append(Message(role="user", content=user_input))

    def build_messages(self) -> list[Message]:
        """Return [system] + full history as a flat list (read-only, no side-effects).

        Always includes the original user message because add_user_input() stores
        it in _history — so multi-turn ReAct loops never lose the user message
        when rebuilding the message list after a tool-call round-trip.
        """
        system = Message(role="system", content=self._system_prompt)
        return [system, *self._history]

    def append_assistant(self, msg: Message) -> None:
        self._history.append(msg)

    def append_tool_result(self, result: ToolResult) -> None:
        msg = Message(
            role="tool",
            content=result.content,
            tool_call_id=result.tool_call_id or "",
        )
        self._history.append(msg)

    def compress_if_needed(self) -> bool:
        """Check token budget and truncate history if needed.

        Returns True if any compression/truncation occurred, False otherwise.
        When no budget calculator is injected (M1 mode), this is a no-op.

        Compression strategy: drop the oldest non-pinned messages until usage
        drops below the 80% threshold.  Pinned messages (the system prompt and
        the most recent user message) are never dropped.

        M3 Note: Full LLM-based summarisation is implemented in compression.py
        and invoked by the AgentLoop before calling this method.  This method
        only performs algorithmic truncation as a safety fallback.
        """
        if self._budget is None:
            return False

        messages = self.build_messages()
        status = self._budget.check(messages)

        from coda.core.budget import BudgetAction

        if status.action == BudgetAction.OK:
            return False

        # Hard truncation: drop oldest messages until we're back under threshold.
        # Never drop the system prompt (index 0) or the most recent user message.
        compressed = False
        while len(self._history) > 1:
            messages = self.build_messages()
            status = self._budget.check(messages)
            if status.action in (BudgetAction.OK, BudgetAction.COMPRESS):
                # COMPRESS is handled by the Compressor in loop.py; stop here.
                break
            # Drop the oldest history entry (index 1 in messages → index 0 in history)
            self._history.pop(0)
            compressed = True

        return compressed

    def token_usage(self) -> tuple[int, int]:
        """Return (used_tokens, budget_tokens).

        When a BudgetCalculator is injected, returns accurate token counts.
        Otherwise falls back to the rough char/4 estimate from M1.
        """
        messages = self.build_messages()

        if self._budget is not None:
            status = self._budget.check(messages)
            return (status.used_tokens, status.budget_tokens)

        if self._count_fn is not None:
            used = self._count_fn(messages)
            return (used, self._context_window)

        # Fallback: rough char/4 estimate
        total_chars = sum(
            len(m.content) if isinstance(m.content, str) else len(str(m.content)) for m in messages
        )
        return (total_chars // 4, self._context_window)

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    @property
    def history(self) -> list[Message]:
        return list(self._history)
