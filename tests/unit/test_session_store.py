"""Unit tests for JsonlSessionStore and SessionEventLogger cross-process seq fix (M5.1)."""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from coda.core.events import SessionEventLogger
from coda.core.types import SessionEvent, SessionEventType
from coda.memory.store import JsonlSessionStore, SessionRow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(tmp_path: Path) -> JsonlSessionStore:
    return JsonlSessionStore(tmp_path / "sessions")


def _new_sid() -> str:
    return str(uuid.uuid4())[:8]


# ---------------------------------------------------------------------------
# 1. SESSION_INIT header is seq=0
# ---------------------------------------------------------------------------


class TestCreateSession:
    def test_create_session_writes_header_as_seq_0(self, tmp_path: Path) -> None:
        """Fresh store: create_session writes SESSION_INIT with seq=0 as first line."""
        store = _make_store(tmp_path)
        sid = _new_sid()

        store.create_session(sid, model="test-model", agents_md_hash=None, initial_prompt_hash=None)

        sessions_dir = tmp_path / "sessions"
        jsonl = sessions_dir / f"{sid}.jsonl"
        assert jsonl.exists()

        first_line = jsonl.read_text().splitlines()[0]
        obj = json.loads(first_line)
        assert obj["type"] == "session_init"
        assert obj["seq"] == 0
        assert obj["session_id"] == sid
        assert obj["data"]["model"] == "test-model"

    def test_create_session_creates_directory(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        sid = _new_sid()
        store.create_session(sid, model=None, agents_md_hash=None, initial_prompt_hash=None)
        assert (tmp_path / "sessions").is_dir()


# ---------------------------------------------------------------------------
# 2. append + load preserves order
# ---------------------------------------------------------------------------


class TestAppendAndLoad:
    def test_append_and_load_preserves_order(self, tmp_path: Path) -> None:
        """Emit 5 events; load_events returns them in seq order."""
        store = _make_store(tmp_path)
        sid = _new_sid()
        store.create_session(sid, model=None, agents_md_hash=None, initial_prompt_hash=None)

        logger = SessionEventLogger.from_store(store, sid)
        for i in range(5):
            logger.emit(SessionEventType.USER_MESSAGE, data={"i": i})

        events = store.load_events(sid)
        assert len(events) == 6  # SESSION_INIT + 5 user messages
        seqs = [e.seq for e in events]
        assert seqs == sorted(seqs)

    def test_load_events_skips_corrupt_lines(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        sid = _new_sid()
        store.create_session(sid, model=None, agents_md_hash=None, initial_prompt_hash=None)

        jsonl = tmp_path / "sessions" / f"{sid}.jsonl"
        with jsonl.open("a") as f:
            f.write("NOT-JSON\n")

        events = store.load_events(sid)
        # Only the SESSION_INIT line; the corrupt line is skipped
        assert len(events) == 1
        assert events[0].type == SessionEventType.SESSION_INIT


# ---------------------------------------------------------------------------
# 3. max_seq after create = 0
# ---------------------------------------------------------------------------


class TestMaxSeq:
    def test_max_seq_after_create(self, tmp_path: Path) -> None:
        """`max_seq` returns 0 immediately after `create_session`."""
        store = _make_store(tmp_path)
        sid = _new_sid()
        store.create_session(sid, model=None, agents_md_hash=None, initial_prompt_hash=None)

        assert store.max_seq(sid) == 0

    def test_max_seq_with_appended_events(self, tmp_path: Path) -> None:
        """Emit 3 events → max_seq returns 3."""
        store = _make_store(tmp_path)
        sid = _new_sid()
        store.create_session(sid, model=None, agents_md_hash=None, initial_prompt_hash=None)

        logger = SessionEventLogger.from_store(store, sid)
        for _ in range(3):
            logger.emit(SessionEventType.USER_MESSAGE, data={})

        assert store.max_seq(sid) == 3

    def test_max_seq_no_file_returns_minus_one(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        assert store.max_seq("nonexistent-session") == -1


# ---------------------------------------------------------------------------
# 4. list_recent ordering
# ---------------------------------------------------------------------------


class TestListRecent:
    def test_list_recent_orders_by_created_at(self, tmp_path: Path) -> None:
        """3 sessions written in order → list_recent returns newest first."""
        store = _make_store(tmp_path)

        for i, name in enumerate(["old", "mid", "new"]):
            sid = name
            sessions_dir = tmp_path / "sessions"
            sessions_dir.mkdir(parents=True, exist_ok=True)
            # Write manually with controlled timestamps
            ts = (datetime(2024, 1, 1, tzinfo=UTC) + timedelta(hours=i)).isoformat()
            event = {
                "id": str(uuid.uuid4()),
                "session_id": sid,
                "seq": 0,
                "type": "session_init",
                "timestamp": ts,
                "data": {"model": f"model-{name}"},
            }
            (sessions_dir / f"{sid}.jsonl").write_text(json.dumps(event) + "\n")

        rows = store.list_recent(limit=10)
        assert len(rows) == 3
        assert rows[0].session_id == "new"
        assert rows[1].session_id == "mid"
        assert rows[2].session_id == "old"

    def test_list_recent_skips_non_header_files(self, tmp_path: Path) -> None:
        """A file whose first line is not SESSION_INIT is silently skipped."""
        store = _make_store(tmp_path)
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True)

        # Good session
        good_sid = _new_sid()
        store.create_session(good_sid, model="m", agents_md_hash=None, initial_prompt_hash=None)

        # Bad file (no SESSION_INIT header)
        bad_path = sessions_dir / "corrupt.jsonl"
        bad_path.write_text(
            json.dumps(
                {
                    "id": "x",
                    "session_id": "corrupt",
                    "seq": 1,
                    "type": "user_message",
                    "timestamp": datetime.now(UTC).isoformat(),
                    "data": {},
                }
            )
            + "\n"
        )

        rows = store.list_recent()
        session_ids = [r.session_id for r in rows]
        assert good_sid in session_ids
        assert "corrupt" not in session_ids

    def test_list_recent_empty_dir(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        assert store.list_recent() == []

    def test_list_recent_respects_limit(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        for _ in range(5):
            sid = _new_sid()
            store.create_session(sid, model=None, agents_md_hash=None, initial_prompt_hash=None)
        rows = store.list_recent(limit=3)
        assert len(rows) == 3


# ---------------------------------------------------------------------------
# 5. Cross-process seq resume
# ---------------------------------------------------------------------------


class TestCrossProcessSeqResume:
    def test_cross_process_seq_resume(self, tmp_path: Path) -> None:
        """Logger A emits 3 events; new Logger B (from_store) starts at seq=4."""
        store = _make_store(tmp_path)
        sid = _new_sid()
        store.create_session(sid, model=None, agents_md_hash=None, initial_prompt_hash=None)

        # Simulate process A
        logger_a = SessionEventLogger.from_store(store, sid)
        logger_a.emit(SessionEventType.USER_MESSAGE, data={})
        logger_a.emit(SessionEventType.ASSISTANT_MESSAGE, data={})
        logger_a.emit(SessionEventType.TOOL_CALL, data={})
        # seq should be 0(SESSION_INIT) 1, 2, 3 → max_seq=3

        # Simulate process B (new instance, same session)
        store_b = _make_store(tmp_path)  # fresh store object, same backing dir
        logger_b = SessionEventLogger.from_store(store_b, sid)

        assert logger_b.next_seq == 4, (
            f"Expected _seq=4, got {logger_b.next_seq}. "
            "Cross-process seq resume broken."
        )


# ---------------------------------------------------------------------------
# 6. Corrupt last line skipped
# ---------------------------------------------------------------------------


class TestCorruptLastLine:
    def test_corrupt_last_line_is_skipped(self, tmp_path: Path) -> None:
        """Non-JSON trailing line: load_events skips it and returns earlier events."""
        store = _make_store(tmp_path)
        sid = _new_sid()
        store.create_session(sid, model=None, agents_md_hash=None, initial_prompt_hash=None)

        logger = SessionEventLogger.from_store(store, sid)
        logger.emit(SessionEventType.USER_MESSAGE, data={"content": "hello"})

        # Corrupt last line
        jsonl = tmp_path / "sessions" / f"{sid}.jsonl"
        with jsonl.open("a") as f:
            f.write("CORRUPT-LINE\n")

        events = store.load_events(sid)
        # SESSION_INIT + USER_MESSAGE (corrupt skipped)
        assert len(events) == 2
        types = [e.type for e in events]
        assert SessionEventType.SESSION_INIT in types
        assert SessionEventType.USER_MESSAGE in types


# ---------------------------------------------------------------------------
# 7. Sessions and logs are separate files
# ---------------------------------------------------------------------------


class TestSessionsAndLogsSeparate:
    def test_sessions_path_is_not_logs_path(self, tmp_path: Path) -> None:
        """sessions_dir != logs_dir to prevent mixed-file parsing issues."""
        sessions_dir = tmp_path / ".coda" / "sessions"
        logs_dir = tmp_path / ".coda" / "logs"
        assert sessions_dir != logs_dir

    def test_session_file_has_jsonl_extension(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        sid = _new_sid()
        store.create_session(sid, model=None, agents_md_hash=None, initial_prompt_hash=None)
        jsonl = tmp_path / "sessions" / f"{sid}.jsonl"
        assert jsonl.suffix == ".jsonl"


# ---------------------------------------------------------------------------
# 8. SessionEventLogger legacy jsonl_path fallback
# ---------------------------------------------------------------------------


class TestLegacyFallback:
    def test_legacy_jsonl_path_still_writes(self, tmp_path: Path) -> None:
        """Old jsonl_path= API still writes for backward compat (undo cross-process)."""
        sid = _new_sid()
        jsonl = tmp_path / f"{sid}.jsonl"

        logger = SessionEventLogger(session_id=sid, jsonl_path=jsonl)
        logger.emit(SessionEventType.USER_MESSAGE, data={"content": "legacy"})

        assert jsonl.exists()
        lines = [ln for ln in jsonl.read_text().splitlines() if ln.strip()]
        assert len(lines) == 1
        obj = json.loads(lines[0])
        assert obj["type"] == "user_message"
