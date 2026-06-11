"""Tests for ChunkAccumulator and AgentLoop streaming wiring (M6.1)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from coda.core.loop import AgentLoop
from coda.core.types import (
    Decision,
    LLMChunk,
    Message,
    ToolCall,
    ToolDef,
    ToolResult,
)
from coda.core.workspace import LocalWorkspaceResolver
from coda.llm.streaming import ChunkAccumulator
from coda.sandbox.firewall import LocalSandboxRunner
from coda.tools.protocols import ToolContext
from coda.tools.registry import ToolRegistry

# ---------------------------------------------------------------------------
# ChunkAccumulator unit tests
# ---------------------------------------------------------------------------


class TestChunkAccumulator:
    def test_text_only_accumulation(self) -> None:
        acc = ChunkAccumulator()
        acc.add(LLMChunk(delta_text="Hello"))
        acc.add(LLMChunk(delta_text=", "))
        acc.add(LLMChunk(delta_text="world!", finish_reason="stop"))

        msg = acc.to_message()
        assert msg.role == "assistant"
        assert msg.content == "Hello, world!"
        assert msg.tool_calls is None
        assert msg.stop_reason == "stop"

    def test_tool_call_fragments_reassembled(self) -> None:
        acc = ChunkAccumulator()
        acc.add(
            LLMChunk(
                delta_tool_calls=[
                    {
                        "index": 0,
                        "id": "call_1",
                        "function": {"name": "read_file", "arguments": '{"pa'},
                    }
                ]
            )
        )
        acc.add(
            LLMChunk(
                delta_tool_calls=[
                    {
                        "index": 0,
                        "id": None,
                        "function": {"name": None, "arguments": 'th": "a.txt"}'},
                    }
                ],
                finish_reason="tool_calls",
            )
        )

        msg = acc.to_message()
        assert msg.tool_calls is not None
        assert len(msg.tool_calls) == 1
        tc = msg.tool_calls[0]
        assert tc.id == "call_1"
        assert tc.name == "read_file"
        assert tc.arguments == {"path": "a.txt"}
        assert msg.stop_reason == "tool_calls"

    def test_multiple_tool_calls_by_index(self) -> None:
        acc = ChunkAccumulator()
        acc.add(
            LLMChunk(
                delta_tool_calls=[
                    {"index": 0, "id": "c0", "function": {"name": "a", "arguments": "{}"}},
                    {"index": 1, "id": "c1", "function": {"name": "b", "arguments": '{"x"'}},
                ]
            )
        )
        acc.add(
            LLMChunk(
                delta_tool_calls=[
                    {"index": 1, "id": None, "function": {"name": None, "arguments": ": 1}"}},
                ]
            )
        )

        msg = acc.to_message()
        assert msg.tool_calls is not None
        assert [tc.name for tc in msg.tool_calls] == ["a", "b"]
        assert msg.tool_calls[0].arguments == {}
        assert msg.tool_calls[1].arguments == {"x": 1}

    def test_usage_capture(self) -> None:
        acc = ChunkAccumulator()
        acc.add(LLMChunk(delta_text="hi"))
        acc.add(
            LLMChunk(
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            )
        )
        assert acc.usage == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    def test_malformed_arguments_preserved_as_raw(self) -> None:
        acc = ChunkAccumulator()
        acc.add(
            LLMChunk(
                delta_tool_calls=[
                    {"index": 0, "id": "c0", "function": {"name": "t", "arguments": '{"oops'}},
                ]
            )
        )
        msg = acc.to_message()
        assert msg.tool_calls is not None
        assert msg.tool_calls[0].arguments == {"_raw": '{"oops'}

    def test_empty_arguments_default_to_empty_dict(self) -> None:
        acc = ChunkAccumulator()
        acc.add(
            LLMChunk(
                delta_tool_calls=[
                    {"index": 0, "id": "c0", "function": {"name": "t", "arguments": None}},
                ]
            )
        )
        msg = acc.to_message()
        assert msg.tool_calls is not None
        assert msg.tool_calls[0].arguments == {}

    def test_mixed_text_and_tool_calls(self) -> None:
        acc = ChunkAccumulator()
        acc.add(LLMChunk(delta_text="Let me check."))
        acc.add(
            LLMChunk(
                delta_tool_calls=[
                    {"index": 0, "id": "c0", "function": {"name": "ls", "arguments": "{}"}},
                ]
            )
        )
        msg = acc.to_message()
        assert msg.content == "Let me check."
        assert msg.tool_calls is not None
        assert msg.tool_calls[0].name == "ls"


# ---------------------------------------------------------------------------
# AgentLoop streaming wiring tests
# ---------------------------------------------------------------------------


def _ctx(tmp_path: Path) -> ToolContext:
    ws = LocalWorkspaceResolver().resolve(explicit=tmp_path)
    sb = LocalSandboxRunner(ws)
    return ToolContext(
        workspace=ws, sandbox=sb, session_id="test", logger=logging.getLogger("test")
    )


class _AutoApprovalManager:
    @property
    def mode(self) -> str:
        return "full_auto"

    async def check(self, tool_call: ToolCall) -> Decision:
        return "allow"

    async def trust_session(self, session_id: str) -> None:
        pass


class EchoParams(BaseModel):
    message: str


class EchoTool:
    definition = ToolDef(name="echo", description="Echo", parameters=EchoParams)
    requires_approval = False

    async def execute(self, args: dict[str, object], ctx: ToolContext) -> ToolResult:
        params = EchoParams.model_validate(args)
        return ToolResult(tool_call_id="", content=params.message)


class _StreamingProvider:
    """Scripted provider that supports streaming.

    Each call to stream_chat pops the next list of chunks from the script.
    chat() raises so tests prove the streaming path is taken.
    """

    name = "fake-streaming"
    model = "fake/streaming"
    supports_streaming = True

    def __init__(self, scripted_chunks: list[list[LLMChunk]]) -> None:
        self._script = list(scripted_chunks)
        self.stream_calls = 0

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None = None,
        **kwargs: Any,
    ) -> Message:
        raise AssertionError("chat() must not be called when streaming is supported")

    def stream_chat(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[LLMChunk]:
        self.stream_calls += 1
        chunks = self._script.pop(0)

        async def _gen() -> AsyncIterator[LLMChunk]:
            for chunk in chunks:
                yield chunk

        return _gen()

    def count_tokens(self, text: str) -> int:
        return len(text) // 4

    def count_message_tokens(self, messages: list[Message]) -> int:
        return 0


@pytest.mark.asyncio
async def test_loop_streams_text_deltas_to_callback(tmp_path: Path) -> None:
    provider = _StreamingProvider(
        [
            [
                LLMChunk(delta_text="The answer "),
                LLMChunk(delta_text="is 42."),
                LLMChunk(finish_reason="stop"),
            ]
        ]
    )
    deltas: list[str] = []
    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry(),
        tool_ctx=_ctx(tmp_path),
        approval=_AutoApprovalManager(),
        on_delta=deltas.append,
    )
    result = await loop.run("what is the answer?")

    assert result.final_text == "The answer is 42."
    assert result.streamed is True
    # Text deltas plus the trailing newline after each streamed call.
    assert deltas == ["The answer ", "is 42.", "\n"]
    assert provider.stream_calls == 1


@pytest.mark.asyncio
async def test_loop_streaming_tool_call_turn(tmp_path: Path) -> None:
    """Tool-call chunks assemble into a working tool call; final answer streams."""
    provider = _StreamingProvider(
        [
            [
                LLMChunk(delta_text="Calling echo."),
                LLMChunk(
                    delta_tool_calls=[
                        {
                            "index": 0,
                            "id": "tc-1",
                            "function": {"name": "echo", "arguments": '{"message"'},
                        }
                    ]
                ),
                LLMChunk(
                    delta_tool_calls=[
                        {
                            "index": 0,
                            "id": None,
                            "function": {"name": None, "arguments": ': "hi"}'},
                        }
                    ],
                    finish_reason="tool_calls",
                ),
            ],
            [
                LLMChunk(delta_text="done"),
                LLMChunk(finish_reason="stop"),
            ],
        ]
    )
    registry = ToolRegistry()
    registry.register(EchoTool())
    deltas: list[str] = []
    loop = AgentLoop(
        provider=provider,
        registry=registry,
        tool_ctx=_ctx(tmp_path),
        approval=_AutoApprovalManager(),
        on_delta=deltas.append,
    )
    result = await loop.run("echo hi")

    assert result.final_text == "done"
    assert result.tool_calls_made == 1
    assert result.streamed is True
    assert provider.stream_calls == 2
    assert "Calling echo." in deltas
    # Tool result was fed back: echo executed with parsed args.
    history = loop.context_manager.history
    tool_msgs = [m for m in history if m.role == "tool"]
    assert tool_msgs and tool_msgs[0].content == "hi"


@pytest.mark.asyncio
async def test_loop_stream_disabled_uses_chat(tmp_path: Path) -> None:
    """LoopConfig(stream=False) forces the non-streaming chat() path."""
    from coda.core.loop import LoopConfig

    class _ChatOnlyProvider(_StreamingProvider):
        async def chat(
            self,
            messages: list[Message],
            tools: list[ToolDef] | None = None,
            **kwargs: Any,
        ) -> Message:
            return Message(role="assistant", content="via chat")

    provider = _ChatOnlyProvider([])
    deltas: list[str] = []
    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry(),
        tool_ctx=_ctx(tmp_path),
        approval=_AutoApprovalManager(),
        config=LoopConfig(stream=False),
        on_delta=deltas.append,
    )
    result = await loop.run("hello")

    assert result.final_text == "via chat"
    assert result.streamed is False
    assert deltas == []
    assert provider.stream_calls == 0


@pytest.mark.asyncio
async def test_loop_provider_without_streaming_falls_back(tmp_path: Path) -> None:
    """Providers lacking supports_streaming use chat() even when stream=True."""

    class _LegacyProvider:
        name = "legacy"
        model = "legacy/x"

        async def chat(
            self,
            messages: list[Message],
            tools: list[ToolDef] | None = None,
            **kwargs: Any,
        ) -> Message:
            return Message(role="assistant", content="legacy answer")

        async def stream_chat(
            self,
            messages: list[Message],
            tools: list[ToolDef] | None = None,
            **kwargs: Any,
        ) -> AsyncIterator[LLMChunk]:
            raise NotImplementedError

        def count_tokens(self, text: str) -> int:
            return 0

        def count_message_tokens(self, messages: list[Message]) -> int:
            return 0

    deltas: list[str] = []
    loop = AgentLoop(
        provider=_LegacyProvider(),
        registry=ToolRegistry(),
        tool_ctx=_ctx(tmp_path),
        approval=_AutoApprovalManager(),
        on_delta=deltas.append,
    )
    result = await loop.run("hello")

    assert result.final_text == "legacy answer"
    assert result.streamed is False
    assert deltas == []
