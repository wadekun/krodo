"""Unit tests for src/krodo/core/compression.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from krodo.core.compression import (
    AlgorithmicCompressor,
    LLMSummaryCompressor,
    _extract_file_paths,
    _last_user_message,
    _pinned_ids,
    make_compressor,
)
from krodo.core.types import Message, SessionEventType, ToolCall

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _user(content: str) -> Message:
    return Message(role="user", content=content)


def _assistant(content: str, tool_calls: list[ToolCall] | None = None) -> Message:
    return Message(role="assistant", content=content, tool_calls=tool_calls)


def _tool_result(content: str, call_id: str = "tc1") -> Message:
    return Message(role="tool", content=content, tool_call_id=call_id)


def _tc(name: str, path: str) -> ToolCall:
    return ToolCall(id="tc1", name=name, arguments={"path": path})


def _make_history(n_rounds: int = 3) -> list[Message]:
    """Create a simple history with *n_rounds* user/assistant/tool_result triples."""
    history: list[Message] = []
    for i in range(n_rounds):
        history.append(_user(f"request {i}"))
        history.append(
            _assistant(
                "",
                tool_calls=[_tc("read_file", f"file{i}.py")],
            )
        )
        history.append(_tool_result(f"content of file{i}.py", call_id=f"tc{i}"))
    return history


# ---------------------------------------------------------------------------
# _extract_file_paths
# ---------------------------------------------------------------------------


class TestExtractFilePaths:
    def test_basic_extraction(self) -> None:
        msgs = [
            _assistant("", tool_calls=[_tc("read_file", "foo.py")]),
        ]
        paths = _extract_file_paths(msgs)
        assert "foo.py" in paths

    def test_deduplication(self) -> None:
        msgs = [
            _assistant("", tool_calls=[_tc("read_file", "foo.py")]),
            _assistant("", tool_calls=[_tc("write_file", "foo.py")]),
        ]
        paths = _extract_file_paths(msgs)
        assert paths.count("foo.py") == 1

    def test_limit_to_five(self) -> None:
        msgs = [
            _assistant(
                "",
                tool_calls=[
                    ToolCall(id=f"tc{i}", name="read_file", arguments={"path": f"file{i}.py"})
                    for i in range(10)
                ],
            )
        ]
        paths = _extract_file_paths(msgs)
        assert len(paths) <= 5

    def test_no_paths_returns_empty(self) -> None:
        msgs = [_user("hello")]
        paths = _extract_file_paths(msgs)
        assert paths == []


# ---------------------------------------------------------------------------
# _last_user_message
# ---------------------------------------------------------------------------


class TestLastUserMessage:
    def test_returns_last_user(self) -> None:
        msgs = [_user("first"), _assistant("hi"), _user("second")]
        last = _last_user_message(msgs)
        assert last is not None
        assert last.content == "second"

    def test_returns_none_when_no_user(self) -> None:
        msgs = [_assistant("hi")]
        assert _last_user_message(msgs) is None


# ---------------------------------------------------------------------------
# _pinned_ids
# ---------------------------------------------------------------------------


class TestPinnedIds:
    def test_system_message_pinned(self) -> None:
        sys_msg = Message(role="system", content="sys")
        msgs = [sys_msg, _user("hi")]
        pinned = _pinned_ids(msgs)
        assert id(sys_msg) in pinned

    def test_last_user_pinned(self) -> None:
        user_msg = _user("last user")
        msgs = [Message(role="system", content=""), _user("first"), user_msg]
        pinned = _pinned_ids(msgs)
        assert id(user_msg) in pinned


# ---------------------------------------------------------------------------
# AlgorithmicCompressor
# ---------------------------------------------------------------------------


class TestAlgorithmicCompressor:
    @pytest.mark.asyncio
    async def test_compresses_tool_results(self) -> None:
        compressor = AlgorithmicCompressor()
        history = _make_history(3)
        new_history, event = await compressor.compress(history, n_rounds=2)
        # At least some tool_result messages should be replaced with [compressed]
        compressed_msgs = [m for m in new_history if m.content == "[compressed]"]
        assert len(compressed_msgs) >= 1

    @pytest.mark.asyncio
    async def test_returns_compression_event(self) -> None:
        compressor = AlgorithmicCompressor()
        history = _make_history(3)
        _, event = await compressor.compress(history, n_rounds=1)
        assert event is not None
        assert event.type == SessionEventType.COMPRESSION
        assert event.data["strategy"] == "algorithmic"

    @pytest.mark.asyncio
    async def test_empty_history_returns_none_event(self) -> None:
        compressor = AlgorithmicCompressor()
        history: list[Message] = []
        new_history, event = await compressor.compress(history)
        assert event is None
        assert new_history == []

    @pytest.mark.asyncio
    async def test_preserves_tool_call_id(self) -> None:
        compressor = AlgorithmicCompressor()
        history = [
            _user("do something"),
            _assistant("", tool_calls=[_tc("read_file", "foo.py")]),
            _tool_result("some content", call_id="my-call-id"),
        ]
        new_history, _ = await compressor.compress(history, n_rounds=2)
        tool_results = [m for m in new_history if m.role == "tool"]
        assert all(m.tool_call_id is not None for m in tool_results)

    @pytest.mark.asyncio
    async def test_no_compression_when_nothing_to_compress(self) -> None:
        compressor = AlgorithmicCompressor()
        # History with only user message (pinned) — nothing to compress
        history = [_user("only message")]
        new_history, event = await compressor.compress(history, n_rounds=2)
        assert event is None


# ---------------------------------------------------------------------------
# LLMSummaryCompressor
# ---------------------------------------------------------------------------


def _make_mock_provider(summary: str = "Summary of actions taken.") -> MagicMock:
    """Return a mock LLMProvider whose chat() returns a summary message."""
    provider = MagicMock()
    provider.chat = AsyncMock(
        return_value=Message(
            role="assistant",
            content=f"<SUMMARY>{summary}</SUMMARY>",
        )
    )
    return provider


class TestLLMSummaryCompressor:
    @pytest.mark.asyncio
    async def test_replaces_old_messages_with_summary(self) -> None:
        provider = _make_mock_provider("Files read: file0.py, file1.py")
        compressor = LLMSummaryCompressor(provider)
        history = _make_history(3)
        original_len = len(history)
        new_history, event = await compressor.compress(history, n_rounds=2)
        # New history should be shorter than original
        assert len(new_history) < original_len

    @pytest.mark.asyncio
    async def test_returns_compression_event(self) -> None:
        provider = _make_mock_provider()
        compressor = LLMSummaryCompressor(provider)
        history = _make_history(2)
        _, event = await compressor.compress(history, n_rounds=1)
        assert event is not None
        assert event.type == SessionEventType.COMPRESSION
        assert event.data["strategy"] == "llm"

    @pytest.mark.asyncio
    async def test_summary_message_injected(self) -> None:
        summary_text = "Edited foo.py and ran tests."
        provider = _make_mock_provider(summary_text)
        compressor = LLMSummaryCompressor(provider)
        history = _make_history(2)
        new_history, _ = await compressor.compress(history, n_rounds=1)
        summary_msgs = [m for m in new_history if "<SUMMARY>" in str(m.content)]
        assert len(summary_msgs) == 1
        assert summary_text in str(summary_msgs[0].content)

    @pytest.mark.asyncio
    async def test_calls_llm_provider_chat(self) -> None:
        provider = _make_mock_provider()
        compressor = LLMSummaryCompressor(provider)
        history = _make_history(2)
        await compressor.compress(history, n_rounds=1)
        provider.chat.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_history_returns_none_event(self) -> None:
        provider = _make_mock_provider()
        compressor = LLMSummaryCompressor(provider)
        _, event = await compressor.compress([], n_rounds=1)
        assert event is None

    @pytest.mark.asyncio
    async def test_raw_summary_used_when_no_tags(self) -> None:
        provider = MagicMock()
        provider.chat = AsyncMock(
            return_value=Message(role="assistant", content="Plain summary without tags.")
        )
        compressor = LLMSummaryCompressor(provider)
        history = _make_history(2)
        new_history, event = await compressor.compress(history, n_rounds=1)
        assert event is not None
        summary_msgs = [m for m in new_history if "Plain summary" in str(m.content)]
        assert len(summary_msgs) == 1

    @pytest.mark.asyncio
    async def test_pinned_last_user_message_preserved(self) -> None:
        provider = _make_mock_provider()
        compressor = LLMSummaryCompressor(provider)
        history = _make_history(3)
        last_user_content = history[-1].content if history[-1].role == "user" else None

        # Ensure last message is user (add one)
        history.append(_user("final user request"))
        new_history, _ = await compressor.compress(history, n_rounds=2)

        user_msgs = [m for m in new_history if m.role == "user"]
        assert any("final user request" in str(m.content) for m in user_msgs)
        _ = last_user_content  # suppress warning


# ---------------------------------------------------------------------------
# make_compressor factory
# ---------------------------------------------------------------------------


class TestMakeCompressor:
    def test_returns_algorithmic_by_default_without_provider(self) -> None:
        compressor = make_compressor(strategy="algorithmic")
        assert isinstance(compressor, AlgorithmicCompressor)

    def test_returns_llm_with_provider(self) -> None:
        provider = _make_mock_provider()
        compressor = make_compressor(strategy="llm", provider=provider)
        assert isinstance(compressor, LLMSummaryCompressor)

    def test_env_var_selects_algorithmic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KRODO_COMPRESS", "algorithmic")
        compressor = make_compressor()
        assert isinstance(compressor, AlgorithmicCompressor)

    def test_env_var_selects_llm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KRODO_COMPRESS", "llm")
        provider = _make_mock_provider()
        compressor = make_compressor(provider=provider)
        assert isinstance(compressor, LLMSummaryCompressor)

    def test_llm_without_provider_raises(self) -> None:
        with pytest.raises(ValueError, match="requires a provider"):
            make_compressor(strategy="llm")

    def test_unknown_strategy_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown compression strategy"):
            make_compressor(strategy="magic")

    def test_default_without_provider_is_algorithmic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KRODO_COMPRESS", raising=False)
        compressor = make_compressor()
        assert isinstance(compressor, AlgorithmicCompressor)

    def test_explicit_strategy_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KRODO_COMPRESS", "llm")
        # Explicitly passing algorithmic should override env
        compressor = make_compressor(strategy="algorithmic")
        assert isinstance(compressor, AlgorithmicCompressor)


# ---------------------------------------------------------------------------
# Integration: InMemoryContextManager + compressor
# ---------------------------------------------------------------------------


class TestContextManagerWithCompressor:
    @pytest.mark.asyncio
    async def test_compress_if_needed_calls_compressor_when_over_budget(self) -> None:
        from krodo.core.budget import BudgetCalculator
        from krodo.core.context import InMemoryContextManager

        call_count = 0

        # First few calls return high usage (over threshold), then low
        def count_fn(msgs: list[Message]) -> int:
            nonlocal call_count
            call_count += 1
            return 99_000 if call_count <= 2 else 100

        calc = BudgetCalculator(model="gpt-4o", count_fn=count_fn)
        compressor = AlgorithmicCompressor()
        ctx = InMemoryContextManager(
            system_prompt="sys",
            budget=calc,
            compressor=compressor,
        )
        for i in range(4):
            ctx.add_user_input(f"message {i}")
            msg = Message(role="tool", content=f"result {i}", tool_call_id=f"tc{i}")
            ctx._history.append(msg)

        event = await ctx.compress_if_needed()
        assert event is not None or len(ctx.history) < 8

    @pytest.mark.asyncio
    async def test_compress_if_needed_no_op_without_budget(self) -> None:
        from krodo.core.context import InMemoryContextManager

        compressor = AlgorithmicCompressor()
        ctx = InMemoryContextManager(
            system_prompt="sys",
            compressor=compressor,
        )
        ctx.add_user_input("hello")
        event = await ctx.compress_if_needed()
        assert event is None

    @pytest.mark.asyncio
    async def test_compress_if_needed_returns_none_when_ok(self) -> None:
        from krodo.core.budget import BudgetCalculator
        from krodo.core.context import InMemoryContextManager

        calc = BudgetCalculator(model="gpt-4o", count_fn=lambda _: 100)
        ctx = InMemoryContextManager(system_prompt="sys", budget=calc)
        ctx.add_user_input("hello")
        event = await ctx.compress_if_needed()
        assert event is None
