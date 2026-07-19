"""Tests for LiteLLMProvider — Message↔LiteLLM dict conversions + async chat."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel

from krodo.core.types import Message, ToolCall, ToolDef, ToolResult
from krodo.llm.litellm_provider import (
    LiteLLMProvider,
    _litellm_to_message,
    _message_to_litellm,
    _tool_result_to_litellm,
    _tooldef_to_litellm,
)

# ---------------------------------------------------------------------------
# _message_to_litellm
# ---------------------------------------------------------------------------


def test_user_message_to_litellm() -> None:
    msg = Message(role="user", content="hello")
    d = _message_to_litellm(msg)
    assert d == {"role": "user", "content": "hello"}


def test_assistant_message_with_tool_calls_to_litellm() -> None:
    msg = Message(
        role="assistant",
        content="",
        tool_calls=[ToolCall(id="tc1", name="read_file", arguments={"path": "foo.py"})],
    )
    d = _message_to_litellm(msg)
    assert d["role"] == "assistant"
    assert len(d["tool_calls"]) == 1
    tc = d["tool_calls"][0]
    assert tc["id"] == "tc1"
    assert tc["function"]["name"] == "read_file"
    assert json.loads(tc["function"]["arguments"]) == {"path": "foo.py"}


def test_tool_result_message_to_litellm() -> None:
    msg = Message(role="tool", content="file contents", tool_call_id="tc1")
    d = _message_to_litellm(msg)
    assert d["role"] == "tool"
    assert d["content"] == "file contents"
    assert d["tool_call_id"] == "tc1"


# ---------------------------------------------------------------------------
# _litellm_to_message
# ---------------------------------------------------------------------------


def _make_litellm_message(**kwargs: Any) -> Any:
    m = MagicMock()
    m.role = kwargs.get("role", "assistant")
    m.content = kwargs.get("content", "")
    m.tool_calls = kwargs.get("tool_calls", None)
    return m


def _make_tool_call_raw(id: str, name: str, arguments: str) -> Any:
    tc = MagicMock()
    tc.id = id
    tc.function.name = name
    tc.function.arguments = arguments
    return tc


def test_litellm_to_message_plain_text() -> None:
    raw = _make_litellm_message(content="Hello world")
    msg = _litellm_to_message(raw)
    assert msg.role == "assistant"
    assert msg.content == "Hello world"
    assert msg.tool_calls is None


def test_litellm_to_message_with_tool_call() -> None:
    raw = _make_litellm_message(
        content=None,
        tool_calls=[_make_tool_call_raw("tc1", "write_file", '{"path": "out.py", "content": "x"}')],
    )
    msg = _litellm_to_message(raw)
    assert msg.tool_calls is not None
    assert len(msg.tool_calls) == 1
    assert msg.tool_calls[0].name == "write_file"
    assert msg.tool_calls[0].arguments == {"path": "out.py", "content": "x"}


def test_litellm_to_message_bad_json_arguments() -> None:
    raw = _make_litellm_message(tool_calls=[_make_tool_call_raw("tc1", "run", "NOT_JSON")])
    msg = _litellm_to_message(raw)
    assert msg.tool_calls is not None
    assert "_raw" in msg.tool_calls[0].arguments


# ---------------------------------------------------------------------------
# _tooldef_to_litellm
# ---------------------------------------------------------------------------


class ReadFileParams(BaseModel):
    path: str
    limit: int | None = None


def test_tooldef_to_litellm() -> None:
    td = ToolDef(name="read_file", description="Read a file", parameters=ReadFileParams)
    d = _tooldef_to_litellm(td)
    assert d["type"] == "function"
    fn = d["function"]
    assert fn["name"] == "read_file"
    assert fn["description"] == "Read a file"
    assert "properties" in fn["parameters"]
    assert "title" not in fn["parameters"]  # stripped


# ---------------------------------------------------------------------------
# _tool_result_to_litellm
# ---------------------------------------------------------------------------


def test_tool_result_to_litellm() -> None:
    result = ToolResult(tool_call_id="tc1", content="ok", is_error=False)
    d = _tool_result_to_litellm(result)
    assert d == {"role": "tool", "tool_call_id": "tc1", "content": "ok"}


# ---------------------------------------------------------------------------
# LiteLLMProvider.chat (mocked litellm.acompletion)
# ---------------------------------------------------------------------------


def _make_acompletion_response(content: str) -> Any:
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.role = "assistant"
    response.choices[0].message.content = content
    response.choices[0].message.tool_calls = None
    return response


@pytest.mark.asyncio
async def test_chat_returns_assistant_message() -> None:
    provider = LiteLLMProvider(model="anthropic/claude-test")
    mock_response = _make_acompletion_response("Hello!")

    with patch(
        "krodo.llm.litellm_provider.litellm.acompletion",
        new_callable=AsyncMock,
        return_value=mock_response,
    ):
        result = await provider.chat(messages=[Message(role="user", content="hi")])

    assert result.role == "assistant"
    assert result.content == "Hello!"


@pytest.mark.asyncio
async def test_chat_passes_api_base_and_key() -> None:
    provider = LiteLLMProvider(
        model="anthropic/claude-test",
        api_base="https://my-proxy.example.com/v1",
        api_key="my-key",
    )
    mock_response = _make_acompletion_response("ok")

    with patch(
        "krodo.llm.litellm_provider.litellm.acompletion",
        new_callable=AsyncMock,
        return_value=mock_response,
    ) as mock_call:
        await provider.chat(messages=[Message(role="user", content="hi")])

    call_kwargs = mock_call.call_args.kwargs
    assert call_kwargs["api_base"] == "https://my-proxy.example.com/v1"
    assert call_kwargs["api_key"] == "my-key"


@pytest.mark.asyncio
async def test_chat_omits_api_base_when_none() -> None:
    provider = LiteLLMProvider(model="openai/gpt-4o")
    mock_response = _make_acompletion_response("ok")

    with patch(
        "krodo.llm.litellm_provider.litellm.acompletion",
        new_callable=AsyncMock,
        return_value=mock_response,
    ) as mock_call:
        await provider.chat(messages=[Message(role="user", content="hi")])

    call_kwargs = mock_call.call_args.kwargs
    assert "api_base" not in call_kwargs
    assert "api_key" not in call_kwargs


# ---------------------------------------------------------------------------
# LiteLLMProvider.stream_chat (mocked)
# ---------------------------------------------------------------------------


def _make_stream_chunk(
    text: str | None = None,
    finish_reason: str | None = None,
) -> Any:
    chunk = MagicMock()
    chunk.choices = [MagicMock()]
    delta = MagicMock()
    delta.content = text
    delta.tool_calls = None
    chunk.choices[0].delta = delta
    chunk.choices[0].finish_reason = finish_reason
    chunk.usage = None
    return chunk


@pytest.mark.asyncio
async def test_stream_chat_yields_chunks() -> None:
    provider = LiteLLMProvider(model="anthropic/claude-test")
    chunks = [
        _make_stream_chunk("Hello"),
        _make_stream_chunk(", world"),
        _make_stream_chunk(finish_reason="stop"),
    ]

    async def _fake_stream() -> Any:
        for c in chunks:
            yield c

    with patch(
        "krodo.llm.litellm_provider.litellm.acompletion",
        new_callable=AsyncMock,
        return_value=_fake_stream(),
    ):
        collected = []
        async for llm_chunk in provider.stream_chat(messages=[Message(role="user", content="hi")]):
            collected.append(llm_chunk)

    texts = [c.delta_text for c in collected if c.delta_text]
    assert "Hello" in texts
    assert ", world" in texts
    finish_reasons = [c.finish_reason for c in collected if c.finish_reason]
    assert "stop" in finish_reasons


# ---------------------------------------------------------------------------
# count_tokens (smoke test — tiktoken may not be exact)
# ---------------------------------------------------------------------------


def test_count_tokens_returns_positive_int() -> None:
    provider = LiteLLMProvider(model="anthropic/claude-test")
    n = provider.count_tokens("Hello world, this is a test sentence.")
    assert isinstance(n, int)
    assert n > 0


# ---------------------------------------------------------------------------
# count_message_tokens
# ---------------------------------------------------------------------------


def test_count_message_tokens_single_user_message() -> None:
    provider = LiteLLMProvider(model="anthropic/claude-test")
    msgs = [Message(role="user", content="Hello world")]
    n = provider.count_message_tokens(msgs)
    assert isinstance(n, int)
    assert n > 0


def test_count_message_tokens_with_tool_calls() -> None:
    provider = LiteLLMProvider(model="anthropic/claude-test")
    msgs = [
        Message(role="user", content="Do something"),
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="tc1", name="read_file", arguments={"path": "foo.py"})],
        ),
    ]
    n = provider.count_message_tokens(msgs)
    assert n > provider.count_message_tokens([Message(role="user", content="Do something")])


def test_count_message_tokens_with_tool_result() -> None:
    provider = LiteLLMProvider(model="anthropic/claude-test")
    msgs = [
        Message(role="tool", content="file contents here", tool_call_id="tc1"),
    ]
    n = provider.count_message_tokens(msgs)
    assert n > 0


def test_count_message_tokens_list_content() -> None:
    provider = LiteLLMProvider(model="anthropic/claude-test")
    msgs = [
        Message(
            role="user",
            content=[{"type": "text", "text": "hello"}, {"type": "text", "text": "world"}],
        ),
    ]
    n = provider.count_message_tokens(msgs)
    assert n > 0


def test_count_message_tokens_multiple_messages_larger_than_single() -> None:
    provider = LiteLLMProvider(model="anthropic/claude-test")
    single = [Message(role="user", content="Hello")]
    multi = [
        Message(role="user", content="Hello"),
        Message(role="assistant", content="Hi there, how can I help?"),
    ]
    assert provider.count_message_tokens(multi) > provider.count_message_tokens(single)


# ---------------------------------------------------------------------------
# stop_reason / finish_reason propagation — M4.6
# ---------------------------------------------------------------------------


def _make_acompletion_response_with_finish(content: str, finish_reason: str) -> Any:
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.role = "assistant"
    response.choices[0].message.content = content
    response.choices[0].message.tool_calls = None
    response.choices[0].finish_reason = finish_reason
    return response


@pytest.mark.asyncio
async def test_chat_preserves_finish_reason_max_tokens() -> None:
    """finish_reason='max_tokens' must be forwarded as Message.stop_reason."""
    provider = LiteLLMProvider(model="anthropic/claude-test")
    mock_response = _make_acompletion_response_with_finish("partial", "max_tokens")

    with patch(
        "krodo.llm.litellm_provider.litellm.acompletion",
        new_callable=AsyncMock,
        return_value=mock_response,
    ):
        result = await provider.chat(messages=[Message(role="user", content="hi")])

    assert result.stop_reason == "max_tokens"


@pytest.mark.asyncio
async def test_chat_preserves_stop_reason_normal() -> None:
    """finish_reason='stop' must be forwarded as Message.stop_reason."""
    provider = LiteLLMProvider(model="anthropic/claude-test")
    mock_response = _make_acompletion_response_with_finish("done", "stop")

    with patch(
        "krodo.llm.litellm_provider.litellm.acompletion",
        new_callable=AsyncMock,
        return_value=mock_response,
    ):
        result = await provider.chat(messages=[Message(role="user", content="hi")])

    assert result.stop_reason == "stop"


def test_message_stop_reason_defaults_none() -> None:
    """Message.stop_reason must default to None (backward compat)."""
    msg = Message(role="user", content="x")
    assert msg.stop_reason is None


# ---------------------------------------------------------------------------
# extra_kwargs forwarding — M4.8 (max_tokens output budget)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_forwards_max_tokens() -> None:
    """LiteLLMProvider(extra_kwargs={'max_tokens': N}) must include N in every
    chat() call so the model's output is not silently capped at provider default.
    """
    provider = LiteLLMProvider(
        model="anthropic/claude-test",
        extra_kwargs={"max_tokens": 8192},
    )
    mock_response = _make_acompletion_response_with_finish("ok", "stop")

    with patch(
        "krodo.llm.litellm_provider.litellm.acompletion",
        new_callable=AsyncMock,
        return_value=mock_response,
    ) as mock_call:
        await provider.chat(messages=[Message(role="user", content="hi")])

    call_kwargs = mock_call.call_args.kwargs
    assert call_kwargs.get("max_tokens") == 8192


# ---------------------------------------------------------------------------
# Prompt caching (Phase 2 M8) — Anthropic cache_control on system message
# ---------------------------------------------------------------------------


def _build_kwargs_with_system(
    provider: LiteLLMProvider, system_text: str = "You are Krodo."
) -> dict[str, Any]:
    """Helper: call _build_kwargs with a system + user message pair."""
    return provider._build_kwargs(  # noqa: SLF001 — test accesses private method
        messages=[
            Message(role="system", content=system_text),
            Message(role="user", content="hi"),
        ],
        tools=None,
    )


def test_prompt_cache_default_on_for_anthropic() -> None:
    """Default prompt_cache=True tags the system message for Anthropic models."""
    provider = LiteLLMProvider(model="anthropic/claude-sonnet-4-5-20250929")
    kwargs = _build_kwargs_with_system(provider)
    assert kwargs["messages"][0]["cache_control"] == {"type": "ephemeral"}
    # Non-system messages are NOT tagged (only the prompt prefix is worth caching)
    assert "cache_control" not in kwargs["messages"][1]


def test_prompt_cache_disabled_no_tag() -> None:
    """prompt_cache=False must not tag any message."""
    provider = LiteLLMProvider(
        model="anthropic/claude-sonnet-4-5-20250929",
        prompt_cache=False,
    )
    kwargs = _build_kwargs_with_system(provider)
    for msg in kwargs["messages"]:
        assert "cache_control" not in msg


def test_prompt_cache_skipped_for_non_anthropic() -> None:
    """OpenAI/Gemini cache provider-side — no cache_control tag from krodo."""
    provider = LiteLLMProvider(model="openai/gpt-4o")
    kwargs = _build_kwargs_with_system(provider)
    for msg in kwargs["messages"]:
        assert "cache_control" not in msg


def test_prompt_cache_noop_without_system_message() -> None:
    """If there's no system message (edge case), cache_control is not added
    and _build_kwargs does not crash."""
    provider = LiteLLMProvider(model="anthropic/claude-sonnet-4-5-20250929")
    kwargs = provider._build_kwargs(  # noqa: SLF001
        messages=[Message(role="user", content="hi")],
        tools=None,
    )
    assert "cache_control" not in kwargs["messages"][0]


# ---------------------------------------------------------------------------
# Second cache breakpoint on the last stable-prefix message (M10 PR2②)
# ---------------------------------------------------------------------------


def _kwargs(provider: LiteLLMProvider, messages: list[Message]) -> dict[str, Any]:
    return provider._build_kwargs(messages=messages, tools=None)  # noqa: SLF001


def test_second_breakpoint_on_repo_map_when_present() -> None:
    """system + <project_memory> + <repo_map> + turn → bp on system AND repo_map."""
    provider = LiteLLMProvider(model="anthropic/claude-sonnet-4-5-20250929")
    kwargs = _kwargs(
        provider,
        [
            Message(role="system", content="sys"),
            Message(role="user", content="<project_memory>\nAGENTS\n</project_memory>"),
            Message(role="user", content="<repo_map>\nmap\n</repo_map>"),
            Message(role="user", content="real question"),
        ],
    )
    msgs = kwargs["messages"]
    assert msgs[0]["cache_control"] == {"type": "ephemeral"}  # system (bp1)
    assert "cache_control" not in msgs[1]  # project_memory — only last prefix tagged
    assert msgs[2]["cache_control"] == {"type": "ephemeral"}  # repo_map (bp2, last prefix)
    assert "cache_control" not in msgs[3]  # real turn


def test_second_breakpoint_falls_back_to_project_memory() -> None:
    """No <repo_map> → bp2 lands on <project_memory>."""
    provider = LiteLLMProvider(model="anthropic/claude-sonnet-4-5-20250929")
    kwargs = _kwargs(
        provider,
        [
            Message(role="system", content="sys"),
            Message(role="user", content="<project_memory>\nAGENTS\n</project_memory>"),
            Message(role="user", content="real question"),
        ],
    )
    msgs = kwargs["messages"]
    assert msgs[0]["cache_control"] == {"type": "ephemeral"}
    assert msgs[1]["cache_control"] == {"type": "ephemeral"}  # project_memory is last prefix
    assert "cache_control" not in msgs[2]


def test_no_second_breakpoint_without_prefix_messages() -> None:
    """system + real turn only → just the system breakpoint, no bp2."""
    provider = LiteLLMProvider(model="anthropic/claude-sonnet-4-5-20250929")
    kwargs = _kwargs(
        provider,
        [Message(role="system", content="sys"), Message(role="user", content="hi")],
    )
    msgs = kwargs["messages"]
    assert msgs[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in msgs[1]


def test_second_breakpoint_skipped_for_non_anthropic() -> None:
    provider = LiteLLMProvider(model="openai/gpt-4o")
    kwargs = _kwargs(
        provider,
        [
            Message(role="system", content="sys"),
            Message(role="user", content="<repo_map>\nm\n</repo_map>"),
            Message(role="user", content="hi"),
        ],
    )
    for msg in kwargs["messages"]:
        assert "cache_control" not in msg
