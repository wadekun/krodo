"""LLM provider Protocol — the sole abstraction that AgentLoop depends on.

All upper-layer code must depend only on LLMProvider, never on LiteLLM
directly.  This allows transparent swap to Bedrock, Vertex, a local model,
or a replay stub in tests.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from coda.core.types import LLMChunk, Message, ToolDef

if TYPE_CHECKING:
    pass


@runtime_checkable
class LLMProvider(Protocol):
    name: str
    model: str

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None = None,
    ) -> Message:
        """Non-streaming single-turn call.  Returns the complete assistant message."""
        ...

    def stream_chat(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None = None,
    ) -> AsyncIterator[LLMChunk]:
        """Return an async iterator of chunks.  The method itself is NOT a coroutine.

        Typical usage::

            async for chunk in provider.stream_chat(messages, tools):
                ...

        Implementations may wrap LiteLLM's CustomStreamWrapper or use
        ``async def stream_chat(...) -> AsyncGenerator[LLMChunk, None]``.
        """
        ...

    def count_tokens(self, text: str) -> int:
        """Estimate token count for *text*.  May be approximate."""
        ...
