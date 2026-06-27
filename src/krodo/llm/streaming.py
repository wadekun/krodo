"""Streaming-chunk accumulation — reassemble LLMChunks into a full Message.

LiteLLM streams tool calls as indexed fragments: the function name usually
arrives on the first fragment for an index, and the JSON ``arguments`` string
arrives split across many fragments that must be concatenated before parsing.
``ChunkAccumulator`` hides that complexity so AgentLoop can treat the streamed
result exactly like a non-streaming ``chat()`` response.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from krodo.core.types import LLMChunk, Message, ToolCall


@dataclass
class _ToolCallFragments:
    """Mutable accumulation slot for one streamed tool call (by index)."""

    id: str | None = None
    name: str | None = None
    argument_parts: list[str] = field(default_factory=list)


def _coerce_usage_int(val: object) -> int:
    """Coerce a loosely-typed ``LLMChunk.usage`` value to int.

    LiteLLM types ``usage`` as ``dict[str, object]``; runtime values are int
    (occasionally str on some providers). We narrow via isinstance so mypy
    stays happy without ``# type: ignore`` and silently default to 0 on
    unexpected types instead of raising.
    """
    if isinstance(val, bool):  # bool is subclass of int — handle explicitly
        return int(val)
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, str):
        try:
            return int(val)
        except ValueError:
            return 0
    return 0


class ChunkAccumulator:
    """Feed ``LLMChunk``s in order; read back a complete assistant ``Message``.

    Mirrors the semantics of ``_litellm_to_message``: malformed argument JSON
    is preserved under ``{"_raw": ...}`` so AgentLoop's BAD_JSON recovery path
    triggers identically for streamed and non-streamed responses.
    """

    def __init__(self) -> None:
        self._text_parts: list[str] = []
        self._tool_fragments: dict[int, _ToolCallFragments] = {}
        self.usage: dict[str, int] | None = None
        self.finish_reason: str | None = None

    def add(self, chunk: LLMChunk) -> None:
        """Merge one streamed chunk into the accumulated state."""
        if chunk.delta_text:
            self._text_parts.append(chunk.delta_text)

        for frag in chunk.delta_tool_calls or []:
            raw_index = frag.get("index")
            index = int(raw_index) if isinstance(raw_index, int | str) and raw_index != "" else 0
            slot = self._tool_fragments.setdefault(index, _ToolCallFragments())

            frag_id = frag.get("id")
            if isinstance(frag_id, str) and frag_id:
                slot.id = frag_id

            function = frag.get("function")
            if isinstance(function, dict):
                name = function.get("name")
                if isinstance(name, str) and name:
                    slot.name = name
                arguments = function.get("arguments")
                if isinstance(arguments, str) and arguments:
                    slot.argument_parts.append(arguments)

        if chunk.usage:
            self.usage = {
                "prompt_tokens": _coerce_usage_int(chunk.usage.get("prompt_tokens", 0)),
                "completion_tokens": _coerce_usage_int(chunk.usage.get("completion_tokens", 0)),
                "total_tokens": _coerce_usage_int(chunk.usage.get("total_tokens", 0)),
            }

        if chunk.finish_reason:
            self.finish_reason = chunk.finish_reason

    @property
    def text(self) -> str:
        return "".join(self._text_parts)

    def to_message(self) -> Message:
        """Build the final assistant Message from everything accumulated."""
        tool_calls: list[ToolCall] | None = None
        if self._tool_fragments:
            tool_calls = []
            for index in sorted(self._tool_fragments):
                slot = self._tool_fragments[index]
                raw_args = "".join(slot.argument_parts)
                arguments: dict[str, object]
                if not raw_args:
                    arguments = {}
                else:
                    try:
                        parsed = json.loads(raw_args)
                        arguments = parsed if isinstance(parsed, dict) else {"_raw": raw_args}
                    except json.JSONDecodeError:
                        arguments = {"_raw": raw_args}
                tool_calls.append(
                    ToolCall(
                        id=slot.id or "",
                        name=slot.name or "",
                        arguments=arguments,
                    )
                )

        return Message(
            role="assistant",
            content=self.text,
            tool_calls=tool_calls,
            stop_reason=self.finish_reason,
            usage=self.usage,
        )


__all__ = ["ChunkAccumulator"]
