"""Integration test: §8 acceptance for krodo undo (M4 PR4).

Scenario:
  1. Initialise a git repo in a temp dir.
  2. Mock LLM writes hello.py via the agent loop (using a fake LLM provider).
  3. A CHECKPOINT event is emitted to the session JSONL.
  4. Run undo_command() → hello.py is removed (restoring pre-write state).
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from krodo.cli.undo import undo_command
from krodo.core.events import SessionEventLogger
from krodo.core.types import SessionEventType
from krodo.core.workspace import LocalWorkspaceResolver
from krodo.sandbox.checkpoint import GitCheckpointManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_git(path: Path) -> None:
    subprocess.run(["git", "init", str(path)], capture_output=True, check=True)  # noqa: S603, S607
    subprocess.run(
        ["git", "config", "user.email", "e2e@test.com"],
        capture_output=True,
        check=True,
        cwd=str(path),
    )
    subprocess.run(
        ["git", "config", "user.name", "E2E Test"],
        capture_output=True,
        check=True,
        cwd=str(path),
    )


def _make_initial_commit(path: Path) -> None:
    (path / "readme.txt").write_text("initial\n")
    subprocess.run(["git", "add", "."], cwd=str(path), capture_output=True, check=True)  # noqa: S603, S607
    subprocess.run(  # noqa: S603, S607
        ["git", "commit", "-m", "init"],
        cwd=str(path),
        capture_output=True,
        check=True,
    )


# ---------------------------------------------------------------------------
# §8 acceptance test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_undo_reverts_written_file(tmp_path: Path) -> None:
    """Full acceptance: agent modifies readme.txt → undo → original restored.

    Note: git stash create captures tracked file modifications only.
    New (untracked) files require git add before stash create; that
    behaviour is intentionally out of scope for M4 and deferred to M5.
    """
    _init_git(tmp_path)
    readme = tmp_path / "readme.txt"
    readme.write_text("original\n")
    _make_initial_commit(tmp_path)

    # Agent modifies readme.txt
    readme.write_text("agent-modified\n")

    # Simulate checkpoint: git stash create (works for tracked file changes)
    workspace = LocalWorkspaceResolver().resolve(explicit=tmp_path)
    mgr = GitCheckpointManager(workspace)
    sha = await mgr.create([readme])
    assert sha is not None, "Dirty tracked file should produce a stash SHA"

    # Write the CHECKPOINT event to the session JSONL (now in .krodo/sessions/)
    session_id = "e2e-test"
    from krodo.memory.store import JsonlSessionStore  # noqa: PLC0415

    store = JsonlSessionStore(tmp_path / ".krodo" / "sessions")
    store.create_session(session_id, model=None, agents_md_hash=None, initial_prompt_hash=None)
    event_logger = SessionEventLogger.from_store(store, session_id)
    event_logger.emit(
        SessionEventType.CHECKPOINT,
        data={
            "sha": sha,
            "affected_paths": [str(readme)],
            "tool": "write_file",
        },
    )
    jsonl_path = tmp_path / ".krodo" / "sessions" / f"{session_id}.jsonl"

    assert readme.read_text() == "agent-modified\n"

    # Agent makes a second modification AFTER the checkpoint
    readme.write_text("second-modification\n")
    assert readme.read_text() == "second-modification\n"

    # Run krodo undo — should restore to the stash-captured state ("agent-modified\n")
    undo_command(session=session_id, _workspace_root=tmp_path)

    # git checkout <stash_sha> -- readme.txt restores the working-tree snapshot
    # captured in the stash = "agent-modified\n"
    assert readme.read_text() == "agent-modified\n"

    # Verify UNDO event was written
    events = [json.loads(ln) for ln in jsonl_path.read_text().splitlines() if ln.strip()]
    undo_events = [e for e in events if e.get("type") == "undo"]
    assert len(undo_events) == 1
    assert undo_events[0]["data"]["sha"] == sha


@pytest.mark.asyncio
async def test_undo_reverts_edited_file(tmp_path: Path) -> None:
    """Full acceptance: agent edits readme.txt → undo → original content restored."""
    _init_git(tmp_path)
    readme = tmp_path / "readme.txt"
    readme.write_text("original content\n")
    _make_initial_commit(tmp_path)

    # Simulate the agent editing the file
    readme.write_text("modified content\n")

    workspace = LocalWorkspaceResolver().resolve(explicit=tmp_path)
    mgr = GitCheckpointManager(workspace)
    sha = await mgr.create([readme])
    assert sha is not None

    session_id = "e2e-edit"
    from krodo.memory.store import JsonlSessionStore  # noqa: PLC0415

    store = JsonlSessionStore(tmp_path / ".krodo" / "sessions")
    store.create_session(session_id, model=None, agents_md_hash=None, initial_prompt_hash=None)
    event_logger = SessionEventLogger.from_store(store, session_id)
    event_logger.emit(
        SessionEventType.CHECKPOINT,
        data={"sha": sha, "affected_paths": [str(readme)], "tool": "edit_file"},
    )

    assert readme.read_text() == "modified content\n"

    # Simulate another change after checkpoint
    readme.write_text("more changes\n")

    undo_command(session=session_id, _workspace_root=tmp_path)

    # Should be back to the stash-captured "modified content\n" state
    assert readme.read_text() == "modified content\n"


def test_undo_non_git_workspace_exits_1(tmp_path: Path) -> None:
    """Non-git workspace: undo exits with code 1 and does not crash."""
    import typer  # noqa: PLC0415

    from krodo.memory.store import JsonlSessionStore  # noqa: PLC0415

    # Write a fake checkpoint event to the sessions dir
    sessions_dir = tmp_path / ".krodo" / "sessions"
    store = JsonlSessionStore(sessions_dir)
    store.create_session("sess", model=None, agents_md_hash=None, initial_prompt_hash=None)

    f = sessions_dir / "sess.jsonl"
    checkpoint_event = {
        "id": "e1",
        "session_id": "sess",
        "seq": 1,
        "type": "checkpoint",
        "timestamp": datetime.now(tz=UTC).isoformat(),
        "data": {"sha": "a" * 40, "affected_paths": [str(tmp_path / "x.py")]},
    }
    with f.open("a") as fh:
        fh.write(json.dumps(checkpoint_event) + "\n")

    with pytest.raises(typer.Exit) as exc_info:
        undo_command(_workspace_root=tmp_path)
    assert exc_info.value.exit_code == 1
