"""LiteLLM-backed LLMProvider implementation.

Design decisions (§4 of the M1 plan):
- api_base / api_key are constructor parameters — no hidden env reads here.
  The CLI reads config once and passes them in; tests can inject fakes.
- stream_chat is a sync method that returns an AsyncGenerator; it is NOT
  itself a coroutine, matching the LLMProvider Protocol signature.
- Message ↔ LiteLLM dict conversion is fully encapsulated in this module.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import litellm

from coda.core.types import LLMChunk, Message, ToolCall, ToolDef, ToolResult


def _message_to_litellm(msg: Message) -> dict[str, Any]:
    """Convert a Coda Message to a LiteLLM-compatible dict."""
    out: dict[str, Any] = {"role": msg.role}

    if msg.role == "tool":
        out["content"] = msg.content if isinstance(msg.content, str) else json.dumps(msg.content)
        if msg.tool_call_id:
            out["tool_call_id"] = msg.tool_call_id
        return out

    if isinstance(msg.content, str):
        out["content"] = msg.content
    else:
        out["content"] = msg.content  # vision / multi-part pass-through

    if msg.tool_calls:
        out["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments),
                },
            }
            for tc in msg.tool_calls
        ]

    return out


def _litellm_to_message(raw: Any, stop_reason: str | None = None) -> Message:
    """Convert a LiteLLM response message to a Coda Message.

    Parameters
    ----------
    raw:
        The LiteLLM response message object (``response.choices[0].message``).
    stop_reason:
        The ``finish_reason`` from ``response.choices[0]``, forwarded verbatim
        so that callers (e.g. AgentLoop) can branch on "max_tokens" etc.
    """
    from typing import Literal, cast

    _raw_role = raw.role or "assistant"
    # Validate against the expected Literal roles; default to "assistant" for unknown values.
    _role: Literal["system", "user", "assistant", "tool"] = cast(
        Literal["system", "user", "assistant", "tool"],
        _raw_role if _raw_role in ("system", "user", "assistant", "tool") else "assistant",
    )
    content: str | list[dict[str, Any]] = raw.content or ""

    tool_calls: list[ToolCall] | None = None
    if raw.tool_calls:
        tool_calls = []
        for tc in raw.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_raw": tc.function.arguments}
            tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))

    return Message(role=_role, content=content, tool_calls=tool_calls, stop_reason=stop_reason)


def _tooldef_to_litellm(td: ToolDef) -> dict[str, Any]:
    """Convert a ToolDef to an OpenAI-style tool dict for LiteLLM."""
    schema = td.parameters.model_json_schema()
    schema.pop("title", None)
    return {
        "type": "function",
        "function": {
            "name": td.name,
            "description": td.description,
            "parameters": schema,
        },
    }


def _tool_result_to_litellm(result: ToolResult) -> dict[str, Any]:
    """Convert a ToolResult to a LiteLLM tool-result message dict."""
    return {
        "role": "tool",
        "tool_call_id": result.tool_call_id,
        "content": result.content,
    }


class LiteLLMProvider:
    """Production LLMProvider backed by LiteLLM.

    Parameters
    ----------
    model:
        LiteLLM model string, e.g. ``"anthropic/claude-sonnet-4-5-20250929"``.
    api_base:
        Override the provider's default base URL (e.g. for a proxy gateway).
    api_key:
        Override the API key.  If None, LiteLLM reads the standard env var
        (e.g. ANTHROPIC_API_KEY).
    extra_kwargs:
        Additional keyword arguments forwarded to every LiteLLM call.
    """

    name = "litellm"

    def __init__(
        self,
        model: str,
        *,
        api_base: str | None = None,
        api_key: str | None = None,
        extra_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self.model = model
        self._api_base = api_base
        self._api_key = api_key
        self._extra: dict[str, Any] = extra_kwargs or {}

    # ------------------------------------------------------------------
    # LLMProvider Protocol implementation
    # ------------------------------------------------------------------

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None = None,
    ) -> Message:
        """Single-turn non-streaming call."""
        kwargs = self._build_kwargs(messages, tools)
        response = await litellm.acompletion(**kwargs, stream=False)
        choice = response.choices[0]
        raw_finish = getattr(choice, "finish_reason", None)
        finish_reason: str | None = raw_finish if isinstance(raw_finish, str) else None
        return _litellm_to_message(choice.message, stop_reason=finish_reason)

    def stream_chat(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None = None,
    ) -> AsyncIterator[LLMChunk]:
        """Return an async iterator of LLMChunk.  The method is NOT async."""
        return self._stream_impl(messages, tools)

    def count_tokens(self, text: str) -> int:
        """Estimate token count using tiktoken (approximate for non-OpenAI models)."""
        try:
            import tiktoken

            enc = tiktoken.encoding_for_model("gpt-4o")
            return len(enc.encode(text))
        except Exception:  # noqa: BLE001
            # Rough fallback: 1 token ≈ 4 chars
            return max(1, len(text) // 4)

    def count_message_tokens(self, messages: list[Message]) -> int:
        """Estimate token count for a full message list.

        Serialises each message's role, content, tool_calls and tool_call_id
        into a plain text representation and counts tokens via tiktoken.
        The +4 per-message overhead approximates the role/separator tokens
        that OpenAI and compatible APIs insert around each message.
        """
        try:
            import tiktoken

            enc = tiktoken.encoding_for_model("gpt-4o")
        except Exception:  # noqa: BLE001
            enc = None

        total = 0
        for msg in messages:
            # Per-message overhead (role + separators ≈ 4 tokens)
            total += 4

            # Content
            if isinstance(msg.content, str):
                text = msg.content
            else:
                text = " ".join(
                    str(part.get("text") or part.get("content") or "") for part in msg.content
                )
            total += self._encode_text(enc, text)

            # tool_call_id (tool result messages)
            if msg.tool_call_id:
                total += self._encode_text(enc, msg.tool_call_id)

            # tool_calls (assistant messages)
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    total += self._encode_text(enc, tc.name)
                    total += self._encode_text(enc, json.dumps(tc.arguments))

        return total

    @staticmethod
    def _encode_text(enc: object, text: str) -> int:
        """Encode *text* with *enc* (tiktoken Encoding) or fall back to char/4."""
        if enc is None:
            return max(1, len(text) // 4)
        try:
            # tiktoken Encoding objects have an encode() method; use getattr to
            # satisfy strict mypy while avoiding a hard tiktoken import here.
            encode_fn = getattr(enc, "encode", None)
            if encode_fn is None:
                return max(1, len(text) // 4)
            return len(encode_fn(text))
        except Exception:  # noqa: BLE001
            return max(1, len(text) // 4)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _stream_impl(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None,
    ) -> AsyncIterator[LLMChunk]:
        kwargs = self._build_kwargs(messages, tools)
        stream = await litellm.acompletion(
            **kwargs, stream=True, stream_options={"include_usage": True}
        )
        async for chunk in stream:
            yield self._parse_chunk(chunk)

    def _build_kwargs(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [_message_to_litellm(m) for m in messages],
        }
        if tools:
            kwargs["tools"] = [_tooldef_to_litellm(td) for td in tools]
        if self._api_base:
            kwargs["api_base"] = self._api_base
        if self._api_key:
            kwargs["api_key"] = self._api_key
        kwargs.update(self._extra)
        return kwargs

    @staticmethod
    def _parse_chunk(chunk: Any) -> LLMChunk:
        delta_text: str | None = None
        delta_tool_calls: list[dict[str, Any]] | None = None
        usage: dict[str, Any] | None = None
        finish_reason: str | None = None

        if chunk.choices:
            choice = chunk.choices[0]
            delta = getattr(choice, "delta", None)
            if delta:
                delta_text = getattr(delta, "content", None) or None
                raw_tcs = getattr(delta, "tool_calls", None)
                if raw_tcs:
                    delta_tool_calls = [
                        {
                            "index": getattr(tc, "index", 0),
                            "id": getattr(tc, "id", None),
                            "function": {
                                "name": getattr(getattr(tc, "function", None), "name", None),
                                "arguments": getattr(
                                    getattr(tc, "function", None), "arguments", None
                                ),
                            },
                        }
                        for tc in raw_tcs
                    ]
            finish_reason = getattr(choice, "finish_reason", None)

        raw_usage = getattr(chunk, "usage", None)
        if raw_usage:
            usage = {
                "prompt_tokens": getattr(raw_usage, "prompt_tokens", 0),
                "completion_tokens": getattr(raw_usage, "completion_tokens", 0),
                "total_tokens": getattr(raw_usage, "total_tokens", 0),
            }

        return LLMChunk(
            delta_text=delta_text,
            delta_tool_calls=delta_tool_calls,
            usage=usage,
            finish_reason=finish_reason,
        )


# Re-export for convenience
__all__ = [
    "LiteLLMProvider",
    "_litellm_to_message",
    "_message_to_litellm",
    "_tool_result_to_litellm",
    "_tooldef_to_litellm",
]
