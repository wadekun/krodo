"""InMemoryContextManager — upgraded in M3 to honour token budgets.

Maintains the full message history in memory.  When a BudgetCalculator is
injected (M3+), compress_if_needed() and token_usage() use real token counts
instead of rough char/4 estimates.

Design principle (separation of concerns):
  - add_user_input()    — **write**: appends the user message to _history.
  - build_messages()    — **read**: returns [system] + _history; no side-effects.
  - append_assistant()  — **write**: appends the assistant message to _history.
  - append_tool_result()— **write**: appends a tool-result message to _history.
  - compress_if_needed()— **async side-effect**: may mutate _history to reduce tokens.

All write methods go through _history so that build_messages() always produces
a complete, valid conversation that satisfies the user→assistant→tool_result
alternation required by Anthropic / OpenAI.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from coda.core.types import Message, SessionEvent, ToolResult

if TYPE_CHECKING:
    from coda.core.budget import BudgetCalculator
    from coda.core.compression import Compressor


class InMemoryContextManager:
    """In-memory context manager with optional token budget enforcement."""

    def __init__(
        self,
        system_prompt: str,
        model_context_window: int = 128_000,
        *,
        budget: BudgetCalculator | None = None,
        compressor: Compressor | None = None,
        count_fn: Callable[[list[Message]], int] | None = None,
    ) -> None:
        self._system_prompt = system_prompt
        self._context_window = model_context_window
        self._history: list[Message] = []
        self._budget = budget
        self._compressor = compressor
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

    async def compress_if_needed(self) -> SessionEvent | None:
        """Check token budget and compress/truncate history if needed.

        Returns a SessionEvent if compression occurred (for the caller to emit),
        or None if no compression was needed.

        When no budget calculator is injected (M1 mode), this is a no-op.

        Compression strategy:
        1. If a Compressor is injected and budget says COMPRESS or TRUNCATE,
           delegate to the Compressor (LLM summary or algorithmic).
        2. If still over threshold after compression, fall back to oldest-first
           hard truncation as a safety net.
        3. Pinned messages (system prompt + most-recent user message) are never
           dropped by the fallback truncation.
        """
        if self._budget is None:
            return None

        messages = self.build_messages()
        status = self._budget.check(messages)

        from coda.core.budget import BudgetAction

        if status.action == BudgetAction.OK:
            return None

        compression_event: SessionEvent | None = None

        # Primary: delegate to the Compressor
        if self._compressor is not None and status.action in (
            BudgetAction.COMPRESS,
            BudgetAction.TRUNCATE,
        ):
            new_history, compression_event = await self._compressor.compress(list(self._history))
            self._history = new_history

            # Re-check budget after compression
            messages = self.build_messages()
            status = self._budget.check(messages)

        # Fallback: hard truncation if still over TRUNCATE threshold
        if status.action == BudgetAction.TRUNCATE:
            while len(self._history) > 1:
                messages = self.build_messages()
                status = self._budget.check(messages)
                if status.action in (BudgetAction.OK, BudgetAction.COMPRESS):
                    break
                self._history.pop(0)

        return compression_event

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
