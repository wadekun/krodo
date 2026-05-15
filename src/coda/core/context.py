"""InMemoryContextManager — M1 minimal implementation.

Maintains the full message history in memory without compression.
Token budget tracking returns rough estimates (tiktoken via LLMProvider).

Design principle (separation of concerns):
  - add_user_input()   — **write**: appends the user message to _history.
  - build_messages()   — **read**: returns [system] + _history; no side-effects.
  - append_assistant() — **write**: appends the assistant message to _history.
  - append_tool_result()— **write**: appends a tool-result message to _history.

All write methods go through _history so that build_messages() always produces
a complete, valid conversation that satisfies the user→assistant→tool_result
alternation required by Anthropic / OpenAI.

M3 will replace compress_if_needed() with the real algorithm from
architecture.md §3.4.1.
"""

from __future__ import annotations

from coda.core.types import Message, ToolResult


class InMemoryContextManager:
    """Simple in-memory context manager — no compression, no persistence."""

    def __init__(
        self,
        system_prompt: str,
        model_context_window: int = 128_000,
    ) -> None:
        self._system_prompt = system_prompt
        self._context_window = model_context_window
        self._history: list[Message] = []

    # ------------------------------------------------------------------
    # ContextManager Protocol implementation
    # ------------------------------------------------------------------

    def add_user_input(self, user_input: str) -> None:
        """Append the user message to history (write, explicit, no side-effects on read)."""
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

    def compress_if_needed(self) -> None:
        """M1 no-op — compression implemented in M3."""
        # TODO(M3): implement token budget algorithm (architecture.md §3.4.1)
        pass  # TODO(M3): compression not yet implemented

    def token_usage(self) -> tuple[int, int]:
        """Rough estimate: (used_chars/4, context_window_tokens)."""
        total_chars = sum(
            len(m.content) if isinstance(m.content, str) else len(str(m.content))
            for m in self._history
        )
        return (total_chars // 4, self._context_window)

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    @property
    def history(self) -> list[Message]:
        return list(self._history)
