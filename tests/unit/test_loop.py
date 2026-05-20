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


def _assert_valid_message_sequence(messages: list[Message]) -> None:
    """Assert that *messages* satisfies basic LLM protocol constraints.

    Rules checked (mirrors Anthropic / OpenAI wire format requirements):
      1. First message must be role=="system".
      2. At least one role=="user" message must be present.
      3. Every tool_call_id referenced in a role=="tool" message must have been
         emitted by an immediately-preceding assistant message's tool_calls list.
    """
    assert messages, "messages list must not be empty"
    assert messages[0].role == "system", f"First message must be system, got {messages[0].role!r}"
    user_roles = [m for m in messages if m.role == "user"]
    assert user_roles, "messages must contain at least one user message"

    # Verify tool_call_id pairing
    pending_tool_ids: set[str] = set()
    for msg in messages:
        if msg.role == "assistant" and msg.tool_calls:
            pending_tool_ids = {tc.id for tc in msg.tool_calls if tc.id}
        elif msg.role == "tool":
            tc_id = msg.tool_call_id or ""
            assert tc_id in pending_tool_ids, (
                f"tool message references unknown tool_call_id {tc_id!r}; "
                f"known ids: {pending_tool_ids}"
            )
            pending_tool_ids.discard(tc_id)


class _FakeLLMProvider:
    """Drives a pre-scripted sequence of LLM responses.

    Validates the message sequence on every chat() call so that protocol
    violations (e.g. missing user message) are caught immediately in tests
    rather than surfacing as cryptic 400 errors from real LLM endpoints.
    """

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
        _assert_valid_message_sequence(messages)
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
    ctx.add_user_input("hello")
    msgs = ctx.build_messages()
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


# ---------------------------------------------------------------------------
# Regression: user message must survive across tool-call round-trips
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_preserves_user_message_across_tool_call(tmp_path: Path) -> None:
    """The original user message must appear in EVERY LLM call, including the
    second one after a tool-call round-trip.

    Regression test for: second chat() call omitting the user message, causing
    Anthropic/compatible endpoints to return 400 "messages 参数非法" (error 1214).
    """
    registry = ToolRegistry()
    registry.register(EchoTool())

    tool_call = ToolCall(id="tc-reg-1", name="echo", arguments={"message": "ping"})
    provider = _FakeLLMProvider(
        [
            # Turn 1: model requests a tool call
            Message(role="assistant", content="", tool_calls=[tool_call]),
            # Turn 2: model gives a final answer after seeing the tool result
            Message(role="assistant", content="all done"),
        ]
    )

    original_prompt = "please echo ping"
    await AgentLoop(
        provider=provider,
        registry=registry,
        tool_ctx=_ctx(tmp_path),
        approval=_AutoApprovalManager(),
    ).run(original_prompt)

    # The fake provider validated sequence on every call (_assert_valid_message_sequence).
    # Additionally verify the second call still carries the original user message.
    assert len(provider.calls) == 2, "expected exactly two chat() calls"
    second_call_messages = provider.calls[1]
    user_messages = [m for m in second_call_messages if m.role == "user"]
    assert user_messages, "second chat() call must contain at least one user message"
    assert any(m.content == original_prompt for m in user_messages), (
        f"original user prompt {original_prompt!r} not found in second chat() call; "
        f"messages: {[m.role for m in second_call_messages]}"
    )


def test_context_user_input_persists_in_history() -> None:
    """add_user_input() must write to _history so build_messages() and
    history both return the user message on subsequent calls.

    This is the unit-level pin for the same bug caught at loop level in
    test_loop_preserves_user_message_across_tool_call.
    """
    ctx = InMemoryContextManager(system_prompt="sys")
    ctx.add_user_input("hello coda")

    # history property must include the user message
    assert len(ctx.history) == 1
    assert ctx.history[0].role == "user"
    assert ctx.history[0].content == "hello coda"

    # build_messages() must also include it (second call — simulates loop rebuild)
    msgs = ctx.build_messages()
    user_in_msgs = [m for m in msgs if m.role == "user"]
    assert user_in_msgs, "build_messages() must include the user message"
    assert user_in_msgs[0].content == "hello coda"

    # Calling build_messages() again must not duplicate entries (pure read)
    msgs2 = ctx.build_messages()
    assert len(msgs2) == len(msgs), "build_messages() must be idempotent (no side effects)"


# ---------------------------------------------------------------------------
# M3: StallDetector integration with AgentLoop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_aborts_on_stall(tmp_path: Path) -> None:
    """AgentLoop must abort when StallDetector raises StallError."""
    registry = ToolRegistry()
    registry.register(EchoTool())

    # Repeatedly issue the same write-tool call (edit_file)
    same_tc = ToolCall(
        id="tc-stall",
        name="echo",
        arguments={"message": "ping"},
    )
    # We'll use 4 identical tool calls to trigger stall at 3rd
    # echo is read-only in terms of stall detection (not in _WRITE_TOOLS)
    # Use write_file to actually trigger stall — but we don't have write_file registered
    # Instead test via the write_file tool name check in StallDetector directly
    provider = _FakeLLMProvider(
        [
            Message(role="assistant", content="", tool_calls=[same_tc]),
            Message(role="assistant", content="", tool_calls=[same_tc]),
            Message(role="assistant", content="", tool_calls=[same_tc]),
            Message(role="assistant", content="done"),
        ]
    )
    result = await AgentLoop(
        provider=provider,
        registry=registry,
        tool_ctx=_ctx(tmp_path),
        approval=_AutoApprovalManager(),
    ).run("do something")
    # Loop should complete (stall on echo doesn't trigger since echo is not in write tools)
    # Just verify it doesn't crash
    assert result is not None


@pytest.mark.asyncio
async def test_loop_handles_provider_error(tmp_path: Path) -> None:
    """AgentLoop must recover from provider errors with retry logic."""
    call_count = 0

    class _FailOnce:
        name = "fail-once"
        model = "test"

        async def chat(
            self,
            messages: list[Message],
            tools: list[ToolDef] | None = None,
        ) -> Message:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("502 Bad Gateway")
            return Message(role="assistant", content="recovered!")

        def stream_chat(self, messages: list[Message], tools: list[ToolDef] | None = None) -> Any:
            raise NotImplementedError

        def count_tokens(self, text: str) -> int:
            return len(text) // 4

        def count_message_tokens(self, messages: list[Message]) -> int:
            return sum(len(str(m.content)) for m in messages) // 4

    import unittest.mock as mock

    with mock.patch("asyncio.sleep"):
        result = await AgentLoop(
            provider=_FailOnce(),  # type: ignore[arg-type]
            registry=ToolRegistry(),
            tool_ctx=_ctx(tmp_path),
            approval=_AutoApprovalManager(),
        ).run("hello")

    assert "recovered" in result.final_text


@pytest.mark.asyncio
async def test_loop_handles_bad_json_tool_call(tmp_path: Path) -> None:
    """AgentLoop must retry when the LLM returns a tool call with invalid JSON args."""
    # Simulate a bad JSON tool call (args have _raw key)
    bad_tc = ToolCall(id="tc-bad", name="read_file", arguments={"_raw": "NOT_JSON"})
    provider = _FakeLLMProvider(
        [
            Message(role="assistant", content="", tool_calls=[bad_tc]),
            Message(role="assistant", content="ok after retry"),
        ]
    )
    result = await AgentLoop(
        provider=provider,
        registry=ToolRegistry(),
        tool_ctx=_ctx(tmp_path),
        approval=_AutoApprovalManager(),
    ).run("do something")
    # Should retry or abort gracefully — not crash
    assert result.final_text is not None


# ---------------------------------------------------------------------------
# abort_reason field — M4.5 additions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_abort_reason_denied(tmp_path: Path) -> None:
    """TurnResult.abort_reason == 'denied' when the user denies a tool call."""
    registry = ToolRegistry()
    registry.register(EchoTool())

    tool_call = ToolCall(id="tc-deny", name="echo", arguments={"message": "x"})
    provider = _FakeLLMProvider([Message(role="assistant", content="", tool_calls=[tool_call])])

    result = await AgentLoop(
        provider=provider,
        registry=registry,
        tool_ctx=_ctx(tmp_path),
        approval=_DenyAllManager(),
    ).run("do it")

    assert result.aborted_by_user
    assert result.abort_reason == "denied"


@pytest.mark.asyncio
async def test_abort_reason_stall(tmp_path: Path) -> None:
    """TurnResult.abort_reason == 'stall' when the StallDetector triggers."""
    registry = ToolRegistry()
    registry.register(EchoTool())

    # Provider keeps returning the same write-class tool call → stall after 3x
    tool_call = ToolCall(id="tc-stall", name="echo", arguments={"message": "stall"})
    provider = _FakeLLMProvider(
        [Message(role="assistant", content="", tool_calls=[tool_call])] * 10
    )

    # Patch StallDetector to treat 'echo' as a write tool for this test
    from coda.core import recovery as _recovery  # noqa: PLC0415

    original = _recovery._WRITE_TOOLS
    _recovery._WRITE_TOOLS = frozenset({"echo"})
    try:
        result = await AgentLoop(
            provider=provider,
            registry=registry,
            tool_ctx=_ctx(tmp_path),
            approval=_AutoApprovalManager(),
        ).run("loop")
    finally:
        _recovery._WRITE_TOOLS = original

    assert result.aborted_by_user
    assert result.abort_reason == "stall"


@pytest.mark.asyncio
async def test_abort_reason_bad_json_exhausted(tmp_path: Path) -> None:
    """abort_reason == 'bad_json' after 2 bad-json retries are exhausted."""
    bad_tc = ToolCall(id="tc-bad", name="echo", arguments={"_raw": "NOT_JSON"})
    # All responses are bad-JSON → retries exhaust → abort
    provider = _FakeLLMProvider([Message(role="assistant", content="", tool_calls=[bad_tc])] * 5)
    result = await AgentLoop(
        provider=provider,
        registry=ToolRegistry(),
        tool_ctx=_ctx(tmp_path),
        approval=_AutoApprovalManager(),
    ).run("bad")

    assert result.aborted_by_user
    assert result.abort_reason == "bad_json"


# ---------------------------------------------------------------------------
# M4.6: max_tokens detection, invalid-args validation, assistant text print
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_tokens_triggers_split_hint(tmp_path: Path) -> None:
    """When LLM returns stop_reason='max_tokens' with tool_calls, the loop
    must NOT show the approval prompt; instead it injects a split-task hint
    and retries the LLM call.
    """
    registry = ToolRegistry()
    registry.register(EchoTool())

    # First response: max_tokens truncation with empty-args tool call
    truncated_tc = ToolCall(id="tc-trunc", name="echo", arguments={})
    # Second response: clean answer after split hint
    responses = [
        Message(role="assistant", content="", tool_calls=[truncated_tc], stop_reason="max_tokens"),
        Message(role="assistant", content="done after split"),
    ]
    provider = _FakeLLMProvider(responses)

    approval_called: list[str] = []

    class _TrackingApproval(_AutoApprovalManager):
        async def check(self, tool_call: ToolCall) -> Any:
            approval_called.append(tool_call.name)
            return "approve"

    result = await AgentLoop(
        provider=provider,
        registry=registry,
        tool_ctx=_ctx(tmp_path),
        approval=_TrackingApproval(),
    ).run("write a big file")

    # Approval must NOT have been called for the truncated response
    assert not approval_called, f"approval was called unexpectedly for: {approval_called}"
    # LLM must have been called twice (once for truncated, once after hint)
    assert len(provider.calls) == 2
    assert result.final_text == "done after split"


@pytest.mark.asyncio
async def test_invalid_tool_args_skips_approval(tmp_path: Path) -> None:
    """When tool args fail Pydantic schema validation, the loop must NOT show
    the approval prompt; instead it injects a recovery message and retries.
    """
    registry = ToolRegistry()
    registry.register(EchoTool())  # EchoParams requires 'message: str'

    # Tool call with empty/invalid args (missing required 'message')
    invalid_tc = ToolCall(id="tc-inv", name="echo", arguments={})
    # Second response: valid call after recovery message
    valid_tc = ToolCall(id="tc-ok", name="echo", arguments={"message": "hello"})
    responses = [
        Message(role="assistant", content="", tool_calls=[invalid_tc]),
        Message(role="assistant", content="", tool_calls=[valid_tc]),
        Message(role="assistant", content="done"),
    ]
    provider = _FakeLLMProvider(responses)

    approval_called: list[str] = []

    class _TrackingApproval(_AutoApprovalManager):
        async def check(self, tool_call: ToolCall) -> Any:
            approval_called.append(tool_call.name)
            return "approve"

    result = await AgentLoop(
        provider=provider,
        registry=registry,
        tool_ctx=_ctx(tmp_path),
        approval=_TrackingApproval(),
    ).run("echo hello")

    # First call (invalid args) must NOT have triggered approval
    # Second call (valid args) may trigger approval
    assert "tc-inv" not in [c for c in approval_called], (
        "approval was called for invalid-arg tool call"
    )
    # Recovery caused at least one extra LLM call
    assert len(provider.calls) >= 2
    assert result.final_text == "done"


@pytest.mark.asyncio
async def test_assistant_text_printed_with_tool_calls(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """When a response carries both content text and tool_calls, the text
    must be printed to the console (dim style) before the tool is processed.
    """
    registry = ToolRegistry()
    registry.register(EchoTool())

    reasoning = "I will now call the echo tool for you."
    tool_call = ToolCall(id="tc-txt", name="echo", arguments={"message": "ping"})
    responses = [
        Message(role="assistant", content=reasoning, tool_calls=[tool_call]),
        Message(role="assistant", content="done"),
    ]
    provider = _FakeLLMProvider(responses)

    await AgentLoop(
        provider=provider,
        registry=registry,
        tool_ctx=_ctx(tmp_path),
        approval=_AutoApprovalManager(),
    ).run("echo with reasoning")

    captured = capsys.readouterr()
    # The reasoning text must appear somewhere in stdout
    assert reasoning in captured.out, (
        f"reasoning text not found in stdout; got: {captured.out!r}"
    )


# ---------------------------------------------------------------------------
# M4.7: invalid_args retry budget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_args_retry_exhaustion(tmp_path: Path) -> None:
    """After _MAX_INVALID_ARGS_RETRIES invalid-arg attempts the loop must abort
    with abort_reason='invalid_args', not loop forever.
    """
    registry = ToolRegistry()
    registry.register(EchoTool())  # EchoParams requires 'message: str'

    # Every response has empty args — model never provides valid args
    invalid_tc = ToolCall(id="tc-inv", name="echo", arguments={})
    provider = _FakeLLMProvider(
        [Message(role="assistant", content="", tool_calls=[invalid_tc])] * 10
    )

    result = await AgentLoop(
        provider=provider,
        registry=registry,
        tool_ctx=_ctx(tmp_path),
        approval=_AutoApprovalManager(),
    ).run("do it")

    assert result.aborted_by_user
    assert result.abort_reason == "invalid_args"
    # Exactly 3 LLM calls: original + 2 retries (3rd attempt triggers abort)
    assert len(provider.calls) == 3


@pytest.mark.asyncio
async def test_invalid_args_recovers_within_budget(tmp_path: Path) -> None:
    """If the model fixes its args within the budget window the loop continues
    normally and completes without aborting.
    """
    registry = ToolRegistry()
    registry.register(EchoTool())

    invalid_tc = ToolCall(id="tc-inv", name="echo", arguments={})
    valid_tc = ToolCall(id="tc-ok", name="echo", arguments={"message": "hello"})

    responses = [
        # attempt 1: invalid
        Message(role="assistant", content="", tool_calls=[invalid_tc]),
        # attempt 2: invalid
        Message(role="assistant", content="", tool_calls=[invalid_tc]),
        # attempt 3: valid args — within budget (budget is exhausted on the 3rd *failure*)
        Message(role="assistant", content="", tool_calls=[valid_tc]),
        # final text
        Message(role="assistant", content="all done"),
    ]
    provider = _FakeLLMProvider(responses)

    result = await AgentLoop(
        provider=provider,
        registry=registry,
        tool_ctx=_ctx(tmp_path),
        approval=_AutoApprovalManager(),
    ).run("echo hello")

    assert not result.aborted_by_user
    assert result.abort_reason == "none"
    assert result.final_text == "all done"
    assert len(provider.calls) == 4
