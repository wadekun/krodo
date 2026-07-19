"""Integration tests — symbol index wiring (Phase 2 M9, PR2).

Exercises the end-to-end invalidation chain that the acceptance criteria pin:

* session start builds the index and emits an ``INDEX_BUILD`` event;
* ``symbol_backend: off`` skips the index entirely (``ToolContext.indexer`` None);
* write tools (``edit_file`` / ``write_file`` / ``apply_patch``) invalidate the
  index so the *next query* reflects new/renamed symbols — without a rebuild.

These tests build real ``ToolContext`` + ``TreeSitterSymbolIndex`` instances and
run the actual tool ``execute`` paths, so they cover the hook → invalidate →
lazy re-extract chain rather than any single unit.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from krodo.cli.main import _build_symbol_index
from krodo.core.events import SessionEventLogger
from krodo.core.workspace import LocalWorkspaceResolver
from krodo.indexer import TreeSitterSymbolIndex
from krodo.memory.store import JsonlSessionStore
from krodo.sandbox.firewall import LocalSandboxRunner
from krodo.sandbox.ignore import KrodoIgnore
from krodo.tools.builtin.fs import EditFileTool, WriteFileTool
from krodo.tools.builtin.patch import ApplyPatchTool
from krodo.tools.protocols import ToolContext

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _workspace(tmp_path: Path) -> object:
    """Resolve a real Workspace rooted at *tmp_path*."""
    return LocalWorkspaceResolver().resolve(explicit=tmp_path)


def _ctx_with_index(tmp_path: Path) -> tuple[ToolContext, TreeSitterSymbolIndex]:
    """Build a ToolContext wired to a freshly-built index over *tmp_path*."""
    ws = _workspace(tmp_path)
    ignore = KrodoIgnore(tmp_path)
    idx = TreeSitterSymbolIndex(
        tmp_path / ".krodo" / "index" / "symbols.db", tmp_path, ignore=ignore
    )
    idx.build_full()
    ctx = ToolContext(
        workspace=ws,  # type: ignore[arg-type]
        sandbox=LocalSandboxRunner(ws),  # type: ignore[arg-type]
        session_id="test",
        logger=logging.getLogger("test"),
        ignore=ignore,
        indexer=idx,
    )
    return ctx, idx


def _event_logger(tmp_path: Path, session_id: str = "s1") -> SessionEventLogger:
    store = JsonlSessionStore(tmp_path / ".krodo" / "sessions")
    store.create_session(session_id, model="m", agents_md_hash="h", initial_prompt_hash=None)
    return SessionEventLogger.from_store(store, session_id)


# ---------------------------------------------------------------------------
# Session build (acceptance #1, #5)
# ---------------------------------------------------------------------------


def test_session_build_indexes_workspace_and_emits_event(tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")

    ws = _workspace(tmp_path)
    ignore = KrodoIgnore(tmp_path)
    store = JsonlSessionStore(tmp_path / ".krodo" / "sessions")
    store.create_session("s1", model="m", agents_md_hash="h", initial_prompt_hash=None)
    elog = SessionEventLogger.from_store(store, "s1")

    idx = _build_symbol_index(ws, ignore, "treesitter", elog, logging.getLogger("test"))  # type: ignore[arg-type]
    assert idx is not None
    assert idx.find_symbol("alpha")  # workspace was indexed at session start

    # INDEX_BUILD event persisted to the session log
    events = store.load_events("s1")
    assert any(e.type.value == "index_build" for e in events)
    build_ev = next(e for e in events if e.type.value == "index_build")
    assert build_ev.data["backend"] == "treesitter"
    assert build_ev.data["symbols"] >= 1


def test_off_mode_returns_none(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    idx = _build_symbol_index(
        ws,
        KrodoIgnore(tmp_path),
        "off",
        _event_logger(tmp_path),
        logging.getLogger("test"),  # type: ignore[arg-type]
    )
    assert idx is None
    # no index database created in off mode
    assert not (tmp_path / ".krodo" / "index" / "symbols.db").exists()


# ---------------------------------------------------------------------------
# Canary defense (M9 closeout): a failed native-crash probe disables the
# index for the session instead of letting build_full() crash the process.
# ---------------------------------------------------------------------------


def test_canary_failure_disables_index_session_continues(tmp_path: Path) -> None:
    """A failed canary probe returns None — no index, but the session is fine."""
    (tmp_path / "mod.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    ws = _workspace(tmp_path)

    with patch("krodo.indexer.canary.probe") as mock_probe:
        mock_probe.return_value = (False, "canary probe exited -11 (1 files sampled)")
        idx = _build_symbol_index(
            ws,
            KrodoIgnore(tmp_path),
            "treesitter",
            _event_logger(tmp_path),
            logging.getLogger("test"),  # type: ignore[arg-type]
        )

    assert idx is None
    mock_probe.assert_called_once()
    # build_full() never runs, so no DB is created either.
    assert not (tmp_path / ".krodo" / "index" / "symbols.db").exists()


def test_canary_success_index_builds_normally(tmp_path: Path) -> None:
    """A successful canary probe still leads to a normal build_full()."""
    (tmp_path / "mod.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    ws = _workspace(tmp_path)

    with patch("krodo.indexer.canary.probe") as mock_probe:
        mock_probe.return_value = (True, None)
        idx = _build_symbol_index(
            ws,
            KrodoIgnore(tmp_path),
            "treesitter",
            _event_logger(tmp_path),
            logging.getLogger("test"),  # type: ignore[arg-type]
        )

    assert idx is not None
    assert idx.find_symbol("alpha")
    idx.close()


# ---------------------------------------------------------------------------
# Write-hook invalidation chain (acceptance #3 — end to end)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_file_rename_reflected_in_index(tmp_path: Path) -> None:
    mod = tmp_path / "mod.py"
    mod.write_text("def alpha():\n    return 1\n", encoding="utf-8")
    ctx, idx = _ctx_with_index(tmp_path)

    assert idx.find_symbol("alpha")
    assert idx.find_symbol("alpha_v2") == []

    result = await EditFileTool().execute(
        {"path": "mod.py", "old_string": "def alpha():", "new_string": "def alpha_v2():"},
        ctx,
    )
    assert not result.is_error

    # No rebuild — the write hook invalidated mod.py; the next query re-extracts.
    assert idx.find_symbol("alpha") == []
    assert idx.find_symbol("alpha_v2")


@pytest.mark.asyncio
async def test_write_file_adds_symbol_reflected_in_index(tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text("def alpha():\n    pass\n", encoding="utf-8")
    ctx, idx = _ctx_with_index(tmp_path)

    result = await WriteFileTool().execute(
        {"path": "new.py", "content": "def brand_new():\n    pass\n"}, ctx
    )
    assert not result.is_error

    assert idx.find_symbol("brand_new")  # newly written file is indexed on next query
    assert idx.find_symbol("alpha")  # pre-existing symbol still there


@pytest.mark.asyncio
async def test_apply_patch_reflected_in_index(tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    ctx, idx = _ctx_with_index(tmp_path)

    patch = (
        "--- a/mod.py\n"
        "+++ b/mod.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-def alpha():\n"
        "+def patched():\n"
        "     return 1\n"
    )
    result = await ApplyPatchTool().execute({"patch": patch}, ctx)
    assert not result.is_error

    assert idx.find_symbol("alpha") == []
    assert idx.find_symbol("patched")


@pytest.mark.asyncio
async def test_indexer_none_skips_hook_cleanly(tmp_path: Path) -> None:
    """When symbol_backend is off, ToolContext.indexer is None and writes succeed."""
    (tmp_path / "mod.py").write_text("def alpha():\n    pass\n", encoding="utf-8")
    ws = _workspace(tmp_path)
    ctx = ToolContext(  # no indexer= (defaults to None)
        workspace=ws,  # type: ignore[arg-type]
        sandbox=LocalSandboxRunner(ws),  # type: ignore[arg-type]
        session_id="test",
        logger=logging.getLogger("test"),
        ignore=KrodoIgnore(tmp_path),
    )
    assert ctx.indexer is None
    result = await EditFileTool().execute(
        {"path": "mod.py", "old_string": "def alpha():", "new_string": "def beta():"}, ctx
    )
    assert not result.is_error  # hook is None-safe; write still works


# ---------------------------------------------------------------------------
# Repo-map injection + refresh (M10 PR2①)
# ---------------------------------------------------------------------------


class _FakeProvider:
    """Just enough of LiteLLMProvider for repo-map rendering (count_tokens)."""

    def count_tokens(self, text: str) -> int:
        return len(text)


def _index_with(root: Path) -> TreeSitterSymbolIndex:
    idx = TreeSitterSymbolIndex(
        root / ".krodo" / "index" / "symbols.db", root, ignore=KrodoIgnore(root)
    )
    idx.build_full()
    return idx


def test_repo_map_injects_after_project_memory(tmp_path: Path) -> None:
    from krodo.cli.main import _build_repo_map_manager
    from krodo.core.context import InMemoryContextManager
    from krodo.core.events import SessionEventLogger
    from krodo.core.types import Message

    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    idx = _index_with(tmp_path)
    ctx = InMemoryContextManager(system_prompt="sys")
    ctx._history.append(  # noqa: SLF001
        Message(role="user", content="<project_memory>\nAGENTS\n</project_memory>")
    )
    elog = SessionEventLogger(session_id="t", store=None)
    mgr = _build_repo_map_manager(idx, True, 2048, _FakeProvider(), ctx, elog)
    try:
        assert mgr is not None
        assert mgr.slot == 1  # right after <project_memory> at [0]
        content = ctx._history[1].content
        assert isinstance(content, str)
        assert content.startswith("<repo_map>")
        assert "alpha" in content
    finally:
        idx.close()


def test_repo_map_injects_at_slot_0_without_project_memory(tmp_path: Path) -> None:
    from krodo.cli.main import _build_repo_map_manager
    from krodo.core.context import InMemoryContextManager
    from krodo.core.events import SessionEventLogger

    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    idx = _index_with(tmp_path)
    ctx = InMemoryContextManager(system_prompt="sys")
    elog = SessionEventLogger(session_id="t", store=None)
    mgr = _build_repo_map_manager(idx, True, 2048, _FakeProvider(), ctx, elog)
    try:
        assert mgr is not None and mgr.slot == 0  # no project_memory → head
        assert ctx._history[0].content.startswith("<repo_map>")  # type: ignore[union-attr]
    finally:
        idx.close()


def test_repo_map_disabled_returns_none(tmp_path: Path) -> None:
    from krodo.cli.main import _build_repo_map_manager
    from krodo.core.context import InMemoryContextManager
    from krodo.core.events import SessionEventLogger

    (tmp_path / "a.py").write_text("def alpha():\n    pass\n", encoding="utf-8")
    idx = _index_with(tmp_path)
    ctx = InMemoryContextManager(system_prompt="sys")
    elog = SessionEventLogger(session_id="t", store=None)
    mgr = _build_repo_map_manager(idx, False, 2048, _FakeProvider(), ctx, elog)
    idx.close()
    assert mgr is None  # repo_map=False → no manager, no injection
    assert ctx._history == []


def test_refresh_repo_map_reflects_write(tmp_path: Path) -> None:
    """refresh_repo_map re-renders <repo_map> after a write bumps the version."""
    import types

    from krodo.cli.main import _build_repo_map_manager, refresh_repo_map
    from krodo.core.context import InMemoryContextManager
    from krodo.core.events import SessionEventLogger
    from krodo.core.types import Message

    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    idx = _index_with(tmp_path)
    ctx = InMemoryContextManager(system_prompt="sys")
    ctx._history.append(  # noqa: SLF001
        Message(role="user", content="<project_memory>\nAGENTS\n</project_memory>")
    )
    elog = SessionEventLogger(session_id="t", store=None)
    mgr = _build_repo_map_manager(idx, True, 2048, _FakeProvider(), ctx, elog)
    assert mgr is not None
    components = types.SimpleNamespace(
        loop=types.SimpleNamespace(context_manager=ctx),
        repo_map_manager=mgr,
        event_logger=elog,
    )
    try:
        before = ctx._history[1].content
        assert isinstance(before, str) and "alpha" in before and "beta" not in before

        # A write adds a new file; invalidate bumps version (optimistic).
        (tmp_path / "b.py").write_text("def beta():\n    pass\n", encoding="utf-8")
        idx.invalidate(["b.py"])

        refresh_repo_map(components)  # type: ignore[arg-type]
        after = ctx._history[1].content
        assert isinstance(after, str)
        assert "beta" in after  # new symbol now reflected, no rebuild
    finally:
        idx.close()
