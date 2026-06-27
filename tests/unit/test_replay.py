"""Unit tests for src/krodo/memory/replay.py — session event replay (M5.2)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from krodo.core.context import InMemoryContextManager
from krodo.core.types import SessionEvent, SessionEventType
from krodo.memory.replay import replay_events

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event(
    event_type: SessionEventType,
    seq: int,
    data: dict | None = None,
    session_id: str = "test-session",
) -> SessionEvent:
    return SessionEvent(
        id=str(uuid.uuid4()),
        session_id=session_id,
        seq=seq,
        type=event_type,
        timestamp=datetime.now(UTC),
        data=data or {},
    )


def _ctx() -> InMemoryContextManager:
    return InMemoryContextManager(system_prompt="You are a helpful assistant.")


# ---------------------------------------------------------------------------
# 1. Rebuild user-assistant history
# ---------------------------------------------------------------------------


class TestReplayRebuildsHistory:
    def test_replay_rebuilds_user_assistant_history(self) -> None:
        """3 turns of user → assistant exchanges replayed correctly."""
        ctx = _ctx()
        events = [
            _event(SessionEventType.SESSION_INIT, 0),
            _event(SessionEventType.USER_MESSAGE, 1, {"content": "Turn 1 user"}),
            _event(SessionEventType.ASSISTANT_MESSAGE, 2, {"content": "Turn 1 assistant"}),
            _event(SessionEventType.USER_MESSAGE, 3, {"content": "Turn 2 user"}),
            _event(SessionEventType.ASSISTANT_MESSAGE, 4, {"content": "Turn 2 assistant"}),
            _event(SessionEventType.USER_MESSAGE, 5, {"content": "Turn 3 user"}),
            _event(SessionEventType.ASSISTANT_MESSAGE, 6, {"content": "Turn 3 assistant"}),
        ]

        stats = replay_events(events, ctx)

        assert stats.turns == 3
        assert stats.messages_restored == 6
        assert not stats.compressed

        history = ctx.history
        assert len(history) == 6
        assert history[0].role == "user"
        assert history[0].content == "Turn 1 user"
        assert history[1].role == "assistant"
        assert history[1].content == "Turn 1 assistant"
        assert history[5].content == "Turn 3 assistant"

    def test_replay_ignores_session_init(self) -> None:
        """SESSION_INIT events are not added to history."""
        ctx = _ctx()
        events = [
            _event(SessionEventType.SESSION_INIT, 0, {"model": "test-model"}),
            _event(SessionEventType.USER_MESSAGE, 1, {"content": "hello"}),
        ]
        replay_events(events, ctx)
        assert len(ctx.history) == 1
        assert ctx.history[0].role == "user"


# ---------------------------------------------------------------------------
# 2. Tool call round-trip
# ---------------------------------------------------------------------------


class TestReplayToolCallRoundTrip:
    def test_replay_handles_tool_call_round_trip(self) -> None:
        """Assistant + tool_result events yield correct alternation."""
        ctx = _ctx()
        tool_call_id = "tc-001"
        events = [
            _event(SessionEventType.USER_MESSAGE, 1, {"content": "read foo.txt"}),
            _event(
                SessionEventType.ASSISTANT_MESSAGE,
                2,
                {
                    "content": "",
                    "tool_calls": [
                        {"id": tool_call_id, "name": "read_file", "arguments": {"path": "foo.txt"}}
                    ],
                },
            ),
            _event(
                SessionEventType.TOOL_RESULT,
                3,
                {
                    "tool_call_id": tool_call_id,
                    "content": "contents of foo.txt",
                    "is_error": False,
                },
            ),
            _event(SessionEventType.ASSISTANT_MESSAGE, 4, {"content": "The file contains: …"}),
        ]

        stats = replay_events(events, ctx)

        assert stats.messages_restored == 4
        history = ctx.history
        assert len(history) == 4
        assert history[0].role == "user"
        assert history[1].role == "assistant"
        assert history[1].tool_calls is not None
        assert history[1].tool_calls[0].name == "read_file"
        assert history[2].role == "tool"
        assert history[2].tool_call_id == tool_call_id
        assert history[3].role == "assistant"
        # arguments round-trip fully (full-fidelity persistence)
        assert history[1].tool_calls[0].arguments == {"path": "foo.txt"}
        assert history[1].tool_calls[0].id == tool_call_id

    def test_replay_legacy_tool_calls_without_arguments(self) -> None:
        """Older sessions persisted only name+id; replay must still rebuild.

        Missing ``arguments`` defaults to ``{}`` so the tool_use/tool_result
        pairing (keyed by id) survives and is re-sent correctly to the LLM.
        """
        ctx = _ctx()
        tool_call_id = "tc-legacy"
        events = [
            _event(SessionEventType.USER_MESSAGE, 1, {"content": "read foo.txt"}),
            _event(
                SessionEventType.ASSISTANT_MESSAGE,
                2,
                {
                    "content": "",
                    # legacy format: no "arguments" key
                    "tool_calls": [{"id": tool_call_id, "name": "read_file"}],
                },
            ),
            _event(
                SessionEventType.TOOL_RESULT,
                3,
                {"tool_call_id": tool_call_id, "content": "contents", "is_error": False},
            ),
        ]

        replay_events(events, ctx)

        history = ctx.history
        assert history[1].tool_calls is not None
        assert len(history[1].tool_calls) == 1
        tc = history[1].tool_calls[0]
        assert tc.name == "read_file"
        assert tc.id == tool_call_id
        assert tc.arguments == {}
        # id preserved so it pairs with the tool result
        assert history[2].tool_call_id == tool_call_id

    def test_replay_tool_call_with_narration_content(self) -> None:
        """ASSISTANT_MESSAGE with both content and tool_calls must restore both.

        This locks in the fix for the persistence bug where narration text
        ("我会帮你创建…") was silently dropped when a message also carried
        tool calls.  Both fields must survive the event-log round-trip.
        """
        ctx = _ctx()
        tool_call_id = "tc-narrate"
        events = [
            _event(SessionEventType.USER_MESSAGE, 1, {"content": "build something"}),
            _event(
                SessionEventType.ASSISTANT_MESSAGE,
                2,
                {
                    "content": "我会帮你创建游戏文件。",
                    "tool_calls": [
                        {"id": tool_call_id, "name": "write_file", "arguments": {"path": "game.js"}}
                    ],
                },
            ),
            _event(
                SessionEventType.TOOL_RESULT,
                3,
                {"tool_call_id": tool_call_id, "content": "ok", "is_error": False},
            ),
        ]

        replay_events(events, ctx)

        asst_msg = ctx.history[1]
        assert asst_msg.content == "我会帮你创建游戏文件。"
        assert asst_msg.tool_calls is not None
        assert asst_msg.tool_calls[0].name == "write_file"
        assert asst_msg.tool_calls[0].arguments == {"path": "game.js"}


# ---------------------------------------------------------------------------
# 3. Compression event
# ---------------------------------------------------------------------------


class TestReplayCompression:
    def test_replay_applies_compression_summary(self) -> None:
        """Compression event replaces preceding replayed history with summary."""
        ctx = _ctx()
        events = [
            _event(SessionEventType.USER_MESSAGE, 1, {"content": "first"}),
            _event(SessionEventType.ASSISTANT_MESSAGE, 2, {"content": "first reply"}),
            _event(
                SessionEventType.COMPRESSION,
                3,
                {"summary": "Summary: user asked 'first', assistant replied."},
            ),
            _event(SessionEventType.USER_MESSAGE, 4, {"content": "second"}),
            _event(SessionEventType.ASSISTANT_MESSAGE, 5, {"content": "second reply"}),
        ]

        stats = replay_events(events, ctx)

        assert stats.compressed
        history = ctx.history
        # After COMPRESSION clears history, we get:
        # [compressed summary as user message, second user, second assistant]
        assert len(history) == 3
        assert "[Context compressed" in history[0].content
        assert "Summary:" in history[0].content
        assert history[1].content == "second"
        assert history[2].content == "second reply"


# ---------------------------------------------------------------------------
# 4. Metadata events skipped
# ---------------------------------------------------------------------------


class TestReplaySkipsMetadata:
    def test_replay_skips_metadata_events(self) -> None:
        """CHECKPOINT / ERROR / UNDO / APPROVAL_DECISION don't pollute history."""
        ctx = _ctx()
        events = [
            _event(SessionEventType.USER_MESSAGE, 1, {"content": "do something"}),
            _event(SessionEventType.CHECKPOINT, 2, {"sha": "abc123", "affected_paths": []}),
            _event(SessionEventType.ERROR, 3, {"message": "timeout"}),
            _event(SessionEventType.APPROVAL_DECISION, 4, {"decision": "approve"}),
            _event(SessionEventType.UNDO, 5, {"sha": "abc123"}),
            _event(SessionEventType.COST_SNAPSHOT, 6, {"total_cost_usd": 0.01}),
            _event(SessionEventType.ASSISTANT_MESSAGE, 7, {"content": "done"}),
        ]

        stats = replay_events(events, ctx)

        # Only user + assistant messages in history
        assert len(ctx.history) == 2
        assert stats.messages_restored == 2
        assert not stats.compressed

    def test_replay_tool_call_event_skipped(self) -> None:
        """TOOL_CALL events are embedded in ASSISTANT_MESSAGE — standalone ones skipped."""
        ctx = _ctx()
        events = [
            _event(SessionEventType.TOOL_CALL, 1, {"name": "read_file", "arguments": {}}),
        ]
        replay_events(events, ctx)
        assert len(ctx.history) == 0

    def test_empty_events_returns_zero_stats(self) -> None:
        ctx = _ctx()
        stats = replay_events([], ctx)
        assert stats.turns == 0
        assert stats.messages_restored == 0
        assert not stats.compressed


# ---------------------------------------------------------------------------
# 5. Healing dangling tool_use (sessions interrupted mid-batch)
# ---------------------------------------------------------------------------


class TestReplayHealsDanglingToolUse:
    def test_dangling_tool_use_at_end_of_stream_gets_skipped_result(self) -> None:
        """Sessions killed mid-batch (e.g. old tool-call-limit bug) self-heal."""
        ctx = _ctx()
        events = [
            _event(SessionEventType.USER_MESSAGE, 1, {"content": "do work"}),
            _event(
                SessionEventType.ASSISTANT_MESSAGE,
                2,
                {
                    "content": "",
                    "tool_calls": [
                        {"id": "tc-done", "name": "read_file", "arguments": {"path": "a"}},
                        {"id": "tc-dangling", "name": "edit_file", "arguments": {"path": "b"}},
                    ],
                },
            ),
            _event(
                SessionEventType.TOOL_RESULT,
                3,
                {"tool_call_id": "tc-done", "content": "ok", "is_error": False},
            ),
            # tc-dangling never got a result — the session was interrupted.
        ]

        replay_events(events, ctx)

        history = ctx.history
        tool_msgs = {m.tool_call_id: m for m in history if m.role == "tool"}
        assert set(tool_msgs) == {"tc-done", "tc-dangling"}
        healed = tool_msgs["tc-dangling"]
        assert "skipped" in healed.content

    def test_dangling_tool_use_healed_before_next_user_message(self) -> None:
        """The synthesized result is inserted BEFORE the following user msg."""
        ctx = _ctx()
        events = [
            _event(SessionEventType.USER_MESSAGE, 1, {"content": "turn one"}),
            _event(
                SessionEventType.ASSISTANT_MESSAGE,
                2,
                {
                    "content": "",
                    "tool_calls": [
                        {"id": "tc-x", "name": "run_shell", "arguments": {"command": "ls"}}
                    ],
                },
            ),
            # No TOOL_RESULT — next turn starts directly.
            _event(SessionEventType.USER_MESSAGE, 3, {"content": "继续"}),
            _event(SessionEventType.ASSISTANT_MESSAGE, 4, {"content": "ok"}),
        ]

        replay_events(events, ctx)

        roles = [m.role for m in ctx.history]
        # user, assistant(tool_calls), tool(synthesized), user, assistant
        assert roles == ["user", "assistant", "tool", "user", "assistant"]
        assert ctx.history[2].tool_call_id == "tc-x"
        assert "skipped" in ctx.history[2].content

    def test_fully_paired_history_is_untouched(self) -> None:
        """Healthy sessions replay without any synthesized results."""
        ctx = _ctx()
        events = [
            _event(SessionEventType.USER_MESSAGE, 1, {"content": "read"}),
            _event(
                SessionEventType.ASSISTANT_MESSAGE,
                2,
                {
                    "content": "",
                    "tool_calls": [{"id": "tc-1", "name": "read_file", "arguments": {}}],
                },
            ),
            _event(
                SessionEventType.TOOL_RESULT,
                3,
                {"tool_call_id": "tc-1", "content": "data", "is_error": False},
            ),
            _event(SessionEventType.ASSISTANT_MESSAGE, 4, {"content": "done"}),
        ]

        stats = replay_events(events, ctx)

        assert stats.messages_restored == 4
        skipped = [m for m in ctx.history if m.role == "tool" and "skipped" in m.content]
        assert not skipped
