"""Unit tests for src/coda/core/events.py — SessionEventLogger."""

from __future__ import annotations

import json
import logging
from datetime import UTC
from pathlib import Path

import pytest

from coda.core.events import SessionEventLogger
from coda.core.types import SessionEvent, SessionEventType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_logger(
    session_id: str = "test-session",
    jsonl_path: Path | None = None,
) -> SessionEventLogger:
    return SessionEventLogger(
        session_id=session_id,
        jsonl_path=jsonl_path,
        logger=logging.getLogger("test"),
    )


def _read_jsonl(path: Path) -> list[SessionEvent]:
    """Parse all JSONL lines and return SessionEvent objects."""
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(SessionEvent.model_validate_json(line))
    return events


# ---------------------------------------------------------------------------
# Basic emit
# ---------------------------------------------------------------------------


class TestSessionEventLoggerEmit:
    def test_emit_returns_session_event(self) -> None:
        logger = _make_logger()
        event = logger.emit(SessionEventType.USER_MESSAGE, data={"content": "hello"})
        assert isinstance(event, SessionEvent)
        assert event.type == SessionEventType.USER_MESSAGE
        assert event.data["content"] == "hello"

    def test_emit_assigns_session_id(self) -> None:
        logger = _make_logger(session_id="my-session-42")
        event = logger.emit(SessionEventType.TOOL_CALL, data={"tool_name": "read_file"})
        assert event.session_id == "my-session-42"

    def test_emit_seq_monotonically_increases(self) -> None:
        logger = _make_logger()
        events = [
            logger.emit(SessionEventType.USER_MESSAGE),
            logger.emit(SessionEventType.ASSISTANT_MESSAGE),
            logger.emit(SessionEventType.TOOL_CALL),
        ]
        seqs = [e.seq for e in events]
        assert seqs == sorted(seqs)
        assert seqs[0] == 0
        assert seqs[1] == 1
        assert seqs[2] == 2

    def test_emit_with_custom_event_id(self) -> None:
        logger = _make_logger()
        custom_id = "00000000-0000-0000-0000-000000000001"
        event = logger.emit(SessionEventType.USER_MESSAGE, event_id=custom_id)
        assert event.id == custom_id

    def test_emit_empty_data_defaults_to_empty_dict(self) -> None:
        logger = _make_logger()
        event = logger.emit(SessionEventType.COMPRESSION)
        assert event.data == {}

    def test_next_seq_increments(self) -> None:
        logger = _make_logger()
        assert logger.next_seq == 0
        logger.emit(SessionEventType.USER_MESSAGE)
        assert logger.next_seq == 1


# ---------------------------------------------------------------------------
# JSONL file roundtrip
# ---------------------------------------------------------------------------


class TestSessionEventLoggerJSONL:
    def test_writes_to_jsonl_file(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "events.jsonl"
        logger = _make_logger(jsonl_path=jsonl)
        logger.emit(SessionEventType.USER_MESSAGE, data={"content": "hello"})
        assert jsonl.exists()

    def test_jsonl_line_is_valid_json(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "events.jsonl"
        logger = _make_logger(jsonl_path=jsonl)
        logger.emit(SessionEventType.TOOL_CALL, data={"tool_name": "read_file"})
        lines = jsonl.read_text().splitlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["type"] == "tool_call"

    def test_multiple_events_are_separate_jsonl_lines(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "events.jsonl"
        logger = _make_logger(jsonl_path=jsonl)
        for i in range(5):
            logger.emit(SessionEventType.TOOL_RESULT, data={"i": i})
        lines = [ln for ln in jsonl.read_text().splitlines() if ln.strip()]
        assert len(lines) == 5

    def test_roundtrip_preserves_all_fields(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "events.jsonl"
        logger = _make_logger(session_id="roundtrip-session", jsonl_path=jsonl)
        original = logger.emit(
            SessionEventType.COMPRESSION,
            data={"strategy": "llm", "messages_compressed": 3},
        )

        events = _read_jsonl(jsonl)
        assert len(events) == 1
        roundtripped = events[0]

        assert roundtripped.id == original.id
        assert roundtripped.session_id == "roundtrip-session"
        assert roundtripped.seq == 0
        assert roundtripped.type == SessionEventType.COMPRESSION
        assert roundtripped.data["strategy"] == "llm"
        assert roundtripped.data["messages_compressed"] == 3

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b" / "c" / "events.jsonl"
        logger = _make_logger(jsonl_path=nested)
        logger.emit(SessionEventType.USER_MESSAGE)
        assert nested.exists()

    def test_no_file_created_when_path_is_none(self) -> None:
        logger = _make_logger()  # jsonl_path=None
        logger.emit(SessionEventType.USER_MESSAGE)
        # No exception, no file created — just logging

    def test_appends_to_existing_file(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "events.jsonl"
        logger = _make_logger(jsonl_path=jsonl)
        logger.emit(SessionEventType.USER_MESSAGE)
        logger.emit(SessionEventType.ASSISTANT_MESSAGE)
        lines = [ln for ln in jsonl.read_text().splitlines() if ln.strip()]
        assert len(lines) == 2


# ---------------------------------------------------------------------------
# emit_from (for Compressor-generated events)
# ---------------------------------------------------------------------------


class TestEmitFrom:
    def test_emit_from_overwrites_session_id(self, tmp_path: Path) -> None:
        from datetime import datetime

        jsonl = tmp_path / "events.jsonl"
        logger = _make_logger(session_id="correct-session", jsonl_path=jsonl)

        foreign_event = SessionEvent(
            id="foreign-id",
            session_id="wrong-session",
            seq=99,
            type=SessionEventType.COMPRESSION,
            timestamp=datetime.now(UTC),
            data={"strategy": "algorithmic"},
        )

        updated = logger.emit_from(foreign_event)
        assert updated.session_id == "correct-session"
        assert updated.seq == 0  # first emit
        assert updated.id == "foreign-id"  # id preserved

    def test_emit_from_written_to_jsonl(self, tmp_path: Path) -> None:
        from datetime import datetime

        jsonl = tmp_path / "events.jsonl"
        logger = _make_logger(jsonl_path=jsonl)

        foreign_event = SessionEvent(
            id="test-id",
            session_id="other",
            seq=0,
            type=SessionEventType.COMPRESSION,
            timestamp=datetime.now(UTC),
            data={"strategy": "llm"},
        )
        logger.emit_from(foreign_event)
        events = _read_jsonl(jsonl)
        assert len(events) == 1
        assert events[0].data["strategy"] == "llm"


# ---------------------------------------------------------------------------
# from_workspace_path factory
# ---------------------------------------------------------------------------


class TestFromWorkspacePath:
    def test_creates_logger_with_correct_path(self, tmp_path: Path) -> None:
        logger = SessionEventLogger.from_workspace_path("abc123", tmp_path)
        assert logger._jsonl_path == tmp_path / ".coda" / "logs" / "abc123.jsonl"

    def test_emits_to_workspace_path(self, tmp_path: Path) -> None:
        logger = SessionEventLogger.from_workspace_path("sess-42", tmp_path)
        logger.emit(SessionEventType.USER_MESSAGE, data={"content": "hi"})
        expected_path = tmp_path / ".coda" / "logs" / "sess-42.jsonl"
        assert expected_path.exists()

    def test_session_id_property(self, tmp_path: Path) -> None:
        logger = SessionEventLogger.from_workspace_path("my-session", tmp_path)
        assert logger.session_id == "my-session"


# ---------------------------------------------------------------------------
# Event types coverage
# ---------------------------------------------------------------------------


class TestAllEventTypes:
    @pytest.mark.parametrize(
        "event_type",
        [
            SessionEventType.USER_MESSAGE,
            SessionEventType.ASSISTANT_MESSAGE,
            SessionEventType.TOOL_CALL,
            SessionEventType.TOOL_RESULT,
            SessionEventType.APPROVAL_DECISION,
            SessionEventType.COMPRESSION,
            SessionEventType.ERROR,
            SessionEventType.COST_SNAPSHOT,
        ],
    )
    def test_each_event_type_can_be_emitted(self, event_type: SessionEventType) -> None:
        logger = _make_logger()
        event = logger.emit(event_type, data={"test": True})
        assert event.type == event_type
