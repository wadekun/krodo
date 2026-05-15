"""InMemoryContextManager — M1 minimal implementation.

Maintains the full message history in memory without compression.
Token budget tracking returns rough estimates (tiktoken via LLMProvider).

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

    def build_messages(self, user_input: str) -> list[Message]:
        """Return the full message list including system prompt + history + new user message."""
        system = Message(role="system", content=self._system_prompt)
        user = Message(role="user", content=user_input)
        return [system, *self._history, user]

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
