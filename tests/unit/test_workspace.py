"""Tests for krodo.core.workspace — Workspace model + LocalWorkspaceResolver.

Covers:
- 5-level discovery priority (flag / env / .krodo marker / .git marker / cwd)
- 4 Workspace invariants (frozen, root is_dir, root writable, source values)
- memory_paths collection
- git_root discovery
- is_path_inside helper
"""

from __future__ import annotations

import os
from datetime import UTC
from pathlib import Path

import pytest

from krodo.core.workspace import LocalWorkspaceResolver, Workspace

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_resolver() -> LocalWorkspaceResolver:
    return LocalWorkspaceResolver()


def _workspace(tmp_path: Path, **kwargs: object) -> Workspace:
    """Return a minimal valid Workspace rooted at tmp_path."""
    from datetime import datetime

    defaults: dict[str, object] = {
        "root": tmp_path,
        "config_path": tmp_path / ".krodo" / "config.yaml",
        "memory_paths": [],
        "git_root": None,
        "source": "cwd",
        "discovered_at": datetime.now(tz=UTC),
    }
    defaults.update(kwargs)
    return Workspace.model_validate(defaults)


# ---------------------------------------------------------------------------
# Priority 1: explicit --root flag
# ---------------------------------------------------------------------------


def test_resolve_flag_takes_priority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    other = tmp_path / "other"
    other.mkdir()
    # Even if KRODO_ROOT is set, explicit flag wins
    monkeypatch.setenv("KRODO_ROOT", str(tmp_path))
    ws = make_resolver().resolve(explicit=other)
    assert ws.root == other
    assert ws.source == "flag"


def test_resolve_flag_expands_home(tmp_path: Path) -> None:
    ws = make_resolver().resolve(explicit=tmp_path)
    assert ws.root == tmp_path.resolve()
    assert ws.source == "flag"


# ---------------------------------------------------------------------------
# Priority 2: KRODO_ROOT env var
# ---------------------------------------------------------------------------


def test_resolve_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KRODO_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)  # No marker in tmp_path
    ws = make_resolver().resolve(explicit=None)
    assert ws.root == tmp_path.resolve()
    assert ws.source == "env"


def test_resolve_env_var_missing_dir_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "nonexistent"
    monkeypatch.setenv("KRODO_ROOT", str(missing))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="not a directory"):
        make_resolver().resolve(explicit=None)


# ---------------------------------------------------------------------------
# Priority 3: .krodo/ marker ancestor
# ---------------------------------------------------------------------------


def test_resolve_krodo_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KRODO_ROOT", raising=False)
    (tmp_path / ".krodo").mkdir()
    subdir = tmp_path / "deep" / "nested"
    subdir.mkdir(parents=True)
    monkeypatch.chdir(subdir)
    ws = make_resolver().resolve(explicit=None)
    assert ws.root == tmp_path
    assert ws.source == "marker"


def test_resolve_krodo_marker_beats_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KRODO_ROOT", raising=False)
    # .git at root, .krodo at child — child wins because we walk upward from CWD
    (tmp_path / ".git").mkdir()
    child = tmp_path / "project"
    child.mkdir()
    (child / ".krodo").mkdir()
    monkeypatch.chdir(child)
    ws = make_resolver().resolve(explicit=None)
    assert ws.root == child
    assert ws.source == "marker"


# ---------------------------------------------------------------------------
# Priority 4: .git/ marker ancestor
# ---------------------------------------------------------------------------


def test_resolve_git_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KRODO_ROOT", raising=False)
    (tmp_path / ".git").mkdir()
    subdir = tmp_path / "src"
    subdir.mkdir()
    monkeypatch.chdir(subdir)
    ws = make_resolver().resolve(explicit=None)
    assert ws.root == tmp_path
    assert ws.source == "marker"


# ---------------------------------------------------------------------------
# Priority 5: CWD fallback
# ---------------------------------------------------------------------------


def test_resolve_cwd_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KRODO_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    ws = make_resolver().resolve(explicit=None)
    assert ws.root == tmp_path.resolve()
    assert ws.source == "cwd"


# ---------------------------------------------------------------------------
# Invariant 1: Workspace is frozen (immutable after construction)
# ---------------------------------------------------------------------------


def test_workspace_is_frozen(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    with pytest.raises(Exception):  # pydantic ValidationError or TypeError
        ws.root = tmp_path / "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Invariant 2: root must be an existing directory
# ---------------------------------------------------------------------------


def test_workspace_rejects_missing_root(tmp_path: Path) -> None:
    from datetime import datetime

    with pytest.raises(ValueError, match="not a directory"):
        Workspace.model_validate(
            {
                "root": tmp_path / "does_not_exist",
                "config_path": tmp_path / ".krodo" / "config.yaml",
                "memory_paths": [],
                "git_root": None,
                "source": "cwd",
                "discovered_at": datetime.now(tz=UTC),
            }
        )


# ---------------------------------------------------------------------------
# Invariant 3: root must be writable
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.getuid() == 0, reason="root user bypasses file permission checks")
def test_workspace_rejects_unwritable_root(tmp_path: Path) -> None:
    read_only = tmp_path / "ro"
    read_only.mkdir(mode=0o555)
    from datetime import datetime

    with pytest.raises(ValueError, match="not writable"):
        Workspace.model_validate(
            {
                "root": read_only,
                "config_path": read_only / ".krodo" / "config.yaml",
                "memory_paths": [],
                "git_root": None,
                "source": "cwd",
                "discovered_at": datetime.now(tz=UTC),
            }
        )
    read_only.chmod(0o755)  # restore for cleanup


# ---------------------------------------------------------------------------
# Invariant 4: source must be one of the four allowed literals
# ---------------------------------------------------------------------------


def test_workspace_rejects_invalid_source(tmp_path: Path) -> None:
    from datetime import datetime

    with pytest.raises(Exception):
        Workspace.model_validate(
            {
                "root": tmp_path,
                "config_path": tmp_path / ".krodo" / "config.yaml",
                "memory_paths": [],
                "git_root": None,
                "source": "magic",  # invalid
                "discovered_at": datetime.now(tz=UTC),
            }
        )


# ---------------------------------------------------------------------------
# memory_paths collection
# ---------------------------------------------------------------------------


def test_memory_paths_includes_existing_agents_md(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("# project")
    (tmp_path / ".krodo").mkdir()
    monkeypatch_obj = pytest.MonkeyPatch()
    monkeypatch_obj.delenv("KRODO_ROOT", raising=False)
    ws = make_resolver().resolve(explicit=tmp_path)
    assert tmp_path / "AGENTS.md" in ws.memory_paths
    monkeypatch_obj.undo()


def test_memory_paths_excludes_missing_agents_md(tmp_path: Path) -> None:
    ws = make_resolver().resolve(explicit=tmp_path)
    assert not any(p.name == "AGENTS.md" and p.parent == tmp_path for p in ws.memory_paths)


# ---------------------------------------------------------------------------
# git_root discovery
# ---------------------------------------------------------------------------


def test_git_root_found_at_ancestor(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    subdir = tmp_path / "src"
    subdir.mkdir()
    ws = make_resolver().resolve(explicit=subdir)
    assert ws.git_root == tmp_path


def test_git_root_none_when_absent(tmp_path: Path) -> None:
    ws = make_resolver().resolve(explicit=tmp_path)
    assert ws.git_root is None


# ---------------------------------------------------------------------------
# is_path_inside helper
# ---------------------------------------------------------------------------


def test_is_path_inside_true(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    assert ws.is_path_inside(tmp_path / "src" / "main.py")


def test_is_path_inside_false_for_parent(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    assert not ws.is_path_inside(tmp_path.parent)


def test_is_path_inside_false_for_sibling(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    sibling = tmp_path.parent / "sibling"
    assert not ws.is_path_inside(sibling)
