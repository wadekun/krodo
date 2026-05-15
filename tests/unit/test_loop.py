"""Tests for AgentLoop and InMemoryContextManager."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from coda.core.context import InMemoryContextManager
from coda.core.loop import AgentLoop, LoopConfig
from coda.core.types import (
    Decision,
    LLMChunk,
    Message,
    ToolCall,
    ToolDef,
    ToolResult,
)
from coda.core.workspace import LocalWorkspaceResolver
from coda.sandbox.firewall import LocalSandboxRunner
from coda.tools.protocols import ToolContext
from coda.tools.registry import ToolRegistry

# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


def _ctx(tmp_path: Path) -> ToolContext:
    ws = LocalWorkspaceResolver().resolve(explicit=tmp_path)
    sb = LocalSandboxRunner(ws)
    return ToolContext(
        workspace=ws, sandbox=sb, session_id="test", logger=logging.getLogger("test")
    )


class _FakeLLMProvider:
    """Drives a pre-scripted sequence of LLM responses."""

    def __init__(self, responses: list[Message]) -> None:
        self._responses = list(responses)
        self._index = 0
        self.calls: list[list[Message]] = []

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None = None,
        **kwargs: Any,
    ) -> Message:
        self.calls.append(messages)
        response = self._responses[self._index]
        self._index = min(self._index + 1, len(self._responses) - 1)
        return response

    async def stream_chat(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[LLMChunk]:
        raise NotImplementedError

    def count_tokens(self, messages: list[Message]) -> int:
        return 0


class _AutoApprovalManager:
    """Approves every tool call automatically."""

    @property
    def mode(self) -> str:
        return "full_auto"

    async def check(self, tool_call: ToolCall) -> Decision:
        return "allow"

    async def trust_session(self, session_id: str) -> None:
        pass


class _DenyAllManager:
    """Denies every tool call."""

    @property
    def mode(self) -> str:
        return "read_only"

    async def check(self, tool_call: ToolCall) -> Decision:
        return "deny"

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


# ---------------------------------------------------------------------------
# InMemoryContextManager tests
# ---------------------------------------------------------------------------


def test_context_build_messages_includes_system_and_user() -> None:
    ctx = InMemoryContextManager(system_prompt="system")
    msgs = ctx.build_messages("hello")
    assert msgs[0].role == "system"
    assert msgs[-1].role == "user"
    assert msgs[-1].content == "hello"


def test_context_append_assistant_grows_history() -> None:
    ctx = InMemoryContextManager(system_prompt="sys")
    ctx.append_assistant(Message(role="assistant", content="ok"))
    assert len(ctx.history) == 1


def test_context_append_tool_result() -> None:
    ctx = InMemoryContextManager(system_prompt="sys")
    ctx.append_tool_result(ToolResult(tool_call_id="tc-1", content="result"))
    assert ctx.history[0].role == "tool"
    assert ctx.history[0].content == "result"


def test_context_token_usage_returns_tuple() -> None:
    ctx = InMemoryContextManager(system_prompt="sys")
    used, limit = ctx.token_usage()
    assert isinstance(used, int)
    assert isinstance(limit, int)
    assert limit > 0


# ---------------------------------------------------------------------------
# AgentLoop — no tool calls (direct final answer)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_direct_answer(tmp_path: Path) -> None:
    """LLM responds with a final text answer immediately — no tool calls."""
    provider = _FakeLLMProvider([Message(role="assistant", content="42")])
    registry = ToolRegistry()
    result = await AgentLoop(
        provider=provider,
        registry=registry,
        tool_ctx=_ctx(tmp_path),
        approval=_AutoApprovalManager(),
    ).run("what is 6*7?")

    assert result.final_text == "42"
    assert result.tool_calls_made == 0
    assert not result.aborted_by_user
    assert not result.hit_tool_call_limit


# ---------------------------------------------------------------------------
# AgentLoop — one round-trip tool call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_one_tool_call(tmp_path: Path) -> None:
    """LLM calls a tool once, then replies with a final answer."""
    registry = ToolRegistry()
    registry.register(EchoTool())

    tool_call = ToolCall(id="tc-1", name="echo", arguments={"message": "hello"})
    responses = [
        Message(role="assistant", content="", tool_calls=[tool_call]),
        Message(role="assistant", content="done"),
    ]
    provider = _FakeLLMProvider(responses)
    result = await AgentLoop(
        provider=provider,
        registry=registry,
        tool_ctx=_ctx(tmp_path),
        approval=_AutoApprovalManager(),
    ).run("echo hello")

    assert result.final_text == "done"
    assert result.tool_calls_made == 1
    assert not result.aborted_by_user


# ---------------------------------------------------------------------------
# AgentLoop — tool call denied by user
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_tool_denied_by_user(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())

    tool_call = ToolCall(id="tc-1", name="echo", arguments={"message": "hello"})
    responses = [
        Message(role="assistant", content="", tool_calls=[tool_call]),
    ]
    provider = _FakeLLMProvider(responses)
    result = await AgentLoop(
        provider=provider,
        registry=registry,
        tool_ctx=_ctx(tmp_path),
        approval=_DenyAllManager(),
    ).run("echo hello")

    assert result.aborted_by_user
    assert result.tool_calls_made == 1  # denied attempt still counts against budget


# ---------------------------------------------------------------------------
# AgentLoop — hit tool-call limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_hits_tool_call_limit(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())

    # Provider always requests the tool → loop should hit the limit
    tool_call = ToolCall(id="tc-1", name="echo", arguments={"message": "x"})
    # Return tool call responses indefinitely (use a cycling fake)
    infinite_responses = [Message(role="assistant", content="", tool_calls=[tool_call])] * 20
    provider = _FakeLLMProvider(infinite_responses)

    config = LoopConfig(max_tool_calls_per_turn=3)
    result = await AgentLoop(
        provider=provider,
        registry=registry,
        tool_ctx=_ctx(tmp_path),
        approval=_AutoApprovalManager(),
        config=config,
    ).run("repeat forever")

    assert result.hit_tool_call_limit
    assert result.tool_calls_made == 3


# ---------------------------------------------------------------------------
# AgentLoop — multi-turn history persists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_multi_turn_history(tmp_path: Path) -> None:
    """Calling run() twice on the same loop preserves history."""
    provider = _FakeLLMProvider(
        [
            Message(role="assistant", content="turn 1 reply"),
            Message(role="assistant", content="turn 2 reply"),
        ]
    )
    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry(),
        tool_ctx=_ctx(tmp_path),
        approval=_AutoApprovalManager(),
    )
    await loop.run("first message")
    await loop.run("second message")

    # History should contain both assistant replies
    history_roles = [m.role for m in loop.context_manager.history]
    assert history_roles.count("assistant") == 2
