"""Tests for cli/undo.py — krodo undo subcommand (M4 PR4)."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
import typer

from krodo.cli.undo import _emit_undo_event, _find_latest_checkpoint, _resolve_jsonl, undo_command
from krodo.core.types import SessionEventType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_git(path: Path) -> None:
    subprocess.run(["git", "init", str(path)], capture_output=True, check=True)  # noqa: S603, S607
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        capture_output=True,
        check=True,
        cwd=str(path),
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        capture_output=True,
        check=True,
        cwd=str(path),
    )


def _write_checkpoint_event(
    jsonl_path: Path,
    sha: str,
    affected_paths: list[str],
    seq: int = 0,
    session_id: str = "testsession",
) -> None:
    """Append a CHECKPOINT event to a JSONL file."""
    event = {
        "id": "evt-001",
        "session_id": session_id,
        "seq": seq,
        "type": "checkpoint",
        "timestamp": datetime.now(tz=UTC).isoformat(),
        "data": {"sha": sha, "affected_paths": affected_paths, "tool": "write_file"},
    }
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    with jsonl_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")


# ---------------------------------------------------------------------------
# _resolve_jsonl
# ---------------------------------------------------------------------------


class TestResolveJsonl:
    def test_returns_none_if_no_logs_dir(self, tmp_path: Path) -> None:
        result = _resolve_jsonl(tmp_path / ".krodo" / "logs", None)
        assert result is None

    def test_returns_none_if_no_jsonl_files(self, tmp_path: Path) -> None:
        logs = tmp_path / ".krodo" / "logs"
        logs.mkdir(parents=True)
        result = _resolve_jsonl(logs, None)
        assert result is None

    def test_returns_session_file_by_name(self, tmp_path: Path) -> None:
        logs = tmp_path / ".krodo" / "logs"
        logs.mkdir(parents=True)
        f = logs / "mysession.jsonl"
        f.write_text("")
        result = _resolve_jsonl(logs, "mysession")
        assert result == f

    def test_returns_most_recent_file_when_no_session(self, tmp_path: Path) -> None:
        logs = tmp_path / ".krodo" / "logs"
        logs.mkdir(parents=True)
        old = logs / "old.jsonl"
        new = logs / "new.jsonl"
        old.write_text("")
        import time

        time.sleep(0.01)
        new.write_text("")
        result = _resolve_jsonl(logs, None)
        assert result == new

    def test_returns_none_for_unknown_session(self, tmp_path: Path) -> None:
        logs = tmp_path / ".krodo" / "logs"
        logs.mkdir(parents=True)
        (logs / "other.jsonl").write_text("")
        result = _resolve_jsonl(logs, "doesnotexist")
        assert result is None


# ---------------------------------------------------------------------------
# _find_latest_checkpoint
# ---------------------------------------------------------------------------


class TestFindLatestCheckpoint:
    def test_returns_none_for_empty_file(self, tmp_path: Path) -> None:
        f = tmp_path / "session.jsonl"
        f.write_text("")
        assert _find_latest_checkpoint(f) is None

    def test_returns_none_if_no_checkpoint_events(self, tmp_path: Path) -> None:
        f = tmp_path / "session.jsonl"
        event = {
            "id": "e1",
            "session_id": "s",
            "seq": 0,
            "type": "user_message",
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "data": {"content": "hello"},
        }
        f.write_text(json.dumps(event) + "\n")
        assert _find_latest_checkpoint(f) is None

    def test_returns_checkpoint_data(self, tmp_path: Path) -> None:
        f = tmp_path / "session.jsonl"
        _write_checkpoint_event(f, sha="abc123" * 6 + "abcd", affected_paths=["/tmp/hello.py"])
        result = _find_latest_checkpoint(f)
        assert result is not None
        assert "abc123" in result["sha"]

    def test_returns_latest_of_multiple_checkpoints(self, tmp_path: Path) -> None:
        f = tmp_path / "session.jsonl"
        _write_checkpoint_event(f, sha="first" + "0" * 35, affected_paths=["/a.py"], seq=0)
        _write_checkpoint_event(f, sha="latest" + "0" * 34, affected_paths=["/b.py"], seq=5)
        result = _find_latest_checkpoint(f)
        assert result is not None
        assert "latest" in result["sha"]

    def test_skips_malformed_lines(self, tmp_path: Path) -> None:
        f = tmp_path / "session.jsonl"
        f.write_text("not json\n")
        _write_checkpoint_event(f, sha="abc" + "0" * 37, affected_paths=["/x.py"], seq=1)
        result = _find_latest_checkpoint(f)
        assert result is not None


# ---------------------------------------------------------------------------
# _emit_undo_event
# ---------------------------------------------------------------------------


class TestEmitUndoEvent:
    def test_writes_undo_event_to_jsonl(self, tmp_path: Path) -> None:
        f = tmp_path / "s.jsonl"
        f.write_text("")
        _emit_undo_event(
            jsonl_path=f,
            session_id="sess1",
            sha="abc" + "0" * 37,
            affected_paths=["/tmp/hello.py"],
        )
        lines = f.read_text().strip().splitlines()
        assert len(lines) == 1
        event = json.loads(lines[0])
        assert event["type"] == SessionEventType.UNDO
        assert event["data"]["sha"].startswith("abc")
        assert "/tmp/hello.py" in event["data"]["affected_paths"]


# ---------------------------------------------------------------------------
# undo_command — non-git workspace
# ---------------------------------------------------------------------------


class TestUndoCommandNonGit:
    def test_exits_1_if_no_logs(self, tmp_path: Path) -> None:
        with pytest.raises(typer.Exit) as exc_info:
            undo_command(_workspace_root=tmp_path)
        assert exc_info.value.exit_code == 1

    def test_exits_1_if_no_checkpoint_in_log(self, tmp_path: Path) -> None:
        # M5.1: sessions now live in .krodo/sessions/, not .krodo/logs/
        sessions = tmp_path / ".krodo" / "sessions"
        sessions.mkdir(parents=True)
        f = sessions / "sess.jsonl"
        # Write a non-checkpoint event
        event = {
            "id": "e1",
            "session_id": "sess",
            "seq": 0,
            "type": "user_message",
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "data": {"content": "hi"},
        }
        f.write_text(json.dumps(event) + "\n")
        with pytest.raises(typer.Exit) as exc_info:
            undo_command(_workspace_root=tmp_path)
        assert exc_info.value.exit_code == 1

    def test_exits_1_for_non_git_workspace_with_checkpoint(self, tmp_path: Path) -> None:
        # M5.1: sessions now live in .krodo/sessions/, not .krodo/logs/
        sessions = tmp_path / ".krodo" / "sessions"
        sessions.mkdir(parents=True)
        f = sessions / "sess.jsonl"
        _write_checkpoint_event(f, sha="a" * 40, affected_paths=[str(tmp_path / "hello.py")])
        # tmp_path is not a git repo → should exit 1 with error message
        with pytest.raises(typer.Exit) as exc_info:
            undo_command(_workspace_root=tmp_path)
        assert exc_info.value.exit_code == 1


# ---------------------------------------------------------------------------
# undo_command — git workspace (full restore)
# ---------------------------------------------------------------------------


class TestUndoCommandGit:
    def test_restore_reverts_tracked_file(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        # Make an initial commit with readme.txt
        readme = tmp_path / "readme.txt"
        readme.write_text("original\n")
        subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True, check=True)  # noqa: S603, S607
        subprocess.run(  # noqa: S603, S607
            ["git", "commit", "-m", "init"],
            cwd=str(tmp_path),
            capture_output=True,
            check=True,
        )

        # Modify the tracked file (so git stash create picks it up)
        readme.write_text("modified\n")

        stash_sha = subprocess.run(  # noqa: S603, S607
            ["git", "stash", "create"],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        assert stash_sha, "dirty tracked file should produce a stash SHA"

        # Write the checkpoint event to the sessions dir (M5.1 migration)
        sessions = tmp_path / ".krodo" / "sessions"
        sessions.mkdir(parents=True)
        f = sessions / "sess.jsonl"
        _write_checkpoint_event(f, sha=stash_sha, affected_paths=[str(readme)])

        # Now run undo — should revert readme.txt to "modified\n" (the stash state)
        undo_command(_workspace_root=tmp_path)

        # README should be the "modified" content from the stash
        assert readme.read_text() == "modified\n"

    def test_undo_emits_undo_event(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        readme = tmp_path / "readme.txt"
        readme.write_text("init\n")
        subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True, check=True)  # noqa: S603, S607
        subprocess.run(  # noqa: S603, S607
            ["git", "commit", "-m", "init"],
            cwd=str(tmp_path),
            capture_output=True,
            check=True,
        )

        # Modify tracked file so stash create captures it
        readme.write_text("modified\n")
        stash_sha = subprocess.run(  # noqa: S603, S607
            ["git", "stash", "create"],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert stash_sha

        # M5.1: sessions now live in .krodo/sessions/, not .krodo/logs/
        sessions = tmp_path / ".krodo" / "sessions"
        sessions.mkdir(parents=True)
        f = sessions / "sess.jsonl"
        _write_checkpoint_event(f, sha=stash_sha, affected_paths=[str(readme)])

        undo_command(_workspace_root=tmp_path)

        lines = f.read_text().strip().splitlines()
        events = [json.loads(ln) for ln in lines]
        undo_events = [e for e in events if e.get("type") == "undo"]
        assert len(undo_events) == 1
        assert undo_events[0]["data"]["sha"] == stash_sha
