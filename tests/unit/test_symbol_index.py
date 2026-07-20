"""Tests for krodo.indexer.symbol_index — SQLite-backed TreeSitterSymbolIndex.

Covers build/reconcile, KrodoIgnore obedience, the two freshness paths
(write-hook ``invalidate`` discovery + query-time external-edit refresh),
SQLite reuse/persistence, incremental no-op rebuild, and pruning.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from krodo.indexer import TreeSitterSymbolIndex
from krodo.sandbox.ignore import KrodoIgnore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A tiny workspace with two Python modules."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "mod.py").write_text(
        "def alpha():\n    return 1\ndef beta():\n    return alpha()\n", encoding="utf-8"
    )
    (pkg / "app.py").write_text("from pkg.mod import alpha\nalpha()\n", encoding="utf-8")
    return tmp_path


def _index(root: Path) -> TreeSitterSymbolIndex:
    idx = TreeSitterSymbolIndex(
        root / ".krodo" / "index" / "symbols.db", root, ignore=KrodoIgnore(root)
    )
    idx.build_full()
    return idx


# ---------------------------------------------------------------------------
# Build / stats / query
# ---------------------------------------------------------------------------


def test_build_and_query(workspace: Path) -> None:
    with _index(workspace) as idx:
        st = idx.stats()
        assert st.backend == "treesitter"
        assert st.files_indexed == 2
        assert st.symbols >= 2  # alpha + beta (and maybe more)
        assert st.db_path.endswith("symbols.db")

        alpha = idx.find_symbol("alpha")
        assert len(alpha) == 1
        assert alpha[0].path == "pkg/mod.py"
        assert alpha[0].kind == "function"

        refs = idx.find_references("alpha")
        ref_paths = {r.path for r in refs}
        assert "pkg/mod.py" in ref_paths  # return alpha()
        assert "pkg/app.py" in ref_paths  # alpha()


def test_unknown_symbol_returns_empty(workspace: Path) -> None:
    with _index(workspace) as idx:
        assert idx.find_symbol("nope") == []
        assert idx.find_references("nope") == []


# ---------------------------------------------------------------------------
# KrodoIgnore obedience
# ---------------------------------------------------------------------------


def test_ignored_directories_are_pruned(tmp_path: Path) -> None:
    (tmp_path / "good.py").write_text("def kept():\n    pass\n", encoding="utf-8")
    nm = tmp_path / "node_modules"
    nm.mkdir()
    (nm / "skipped.py").write_text("def vendored():\n    pass\n", encoding="utf-8")

    with _index(tmp_path) as idx:
        assert idx.find_symbol("kept")
        assert idx.find_symbol("vendored") == []  # node_modules pruned
        assert idx.stats().files_indexed == 1


def test_ignored_secret_file_not_indexed(tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text("def alpha():\n    pass\n", encoding="utf-8")
    # .env is in the hardcoded ignore tier.
    (tmp_path / ".env").write_text("SECRET=leak\n", encoding="utf-8")

    with _index(tmp_path) as idx:
        assert idx.find_symbol("alpha")
        assert idx.stats().files_indexed == 1


# ---------------------------------------------------------------------------
# Freshness path A — write-hook invalidate discovers new symbols
# ---------------------------------------------------------------------------


def test_invalidate_discovers_new_symbol(workspace: Path) -> None:
    mod = workspace / "pkg" / "mod.py"
    with _index(workspace) as idx:
        assert idx.find_symbol("gamma") == []
        # edit_file writes new content then fires invalidate(["pkg/mod.py"])
        mod.write_text("def alpha():\n    return 1\ndef gamma():\n    pass\n", encoding="utf-8")
        idx.invalidate(["pkg/mod.py"])
        gamma = idx.find_symbol("gamma")
        assert len(gamma) == 1
        assert gamma[0].line == 3


def test_invalidate_accepts_path_objects(workspace: Path) -> None:
    with _index(workspace) as idx:
        (workspace / "pkg" / "mod.py").write_text("def delta():\n    pass\n", encoding="utf-8")
        idx.invalidate([Path("pkg/mod.py")])
        assert idx.find_symbol("delta")


def test_invalidate_accepts_absolute_path(workspace: Path) -> None:
    """PR2 write tools hold absolute paths — invalidate must normalize them.

    Without normalization, ``self._root / abs_path`` silently replaces the left
    operand and the same file ends up indexed under both relative and absolute
    keys (duplicate query results). This is the regression guard for that bug.
    """
    mod = workspace / "pkg" / "mod.py"
    with _index(workspace) as idx:
        mod.write_text("def gamma():\n    pass\n", encoding="utf-8")
        idx.invalidate([str(mod)])  # absolute, exactly as fs.py would pass
        gamma = idx.find_symbol("gamma")
        assert len(gamma) == 1
        assert gamma[0].path == "pkg/mod.py"  # stored relative — no duplicate row
        # sanity: only one row for that file in the symbols table
        rows = idx._conn.execute(  # noqa: SLF001 — white-box row count
            "SELECT COUNT(*) AS c FROM symbols WHERE path = ?", ("pkg/mod.py",)
        ).fetchone()
        assert rows["c"] == 1


def test_invalidate_drops_path_outside_workspace(workspace: Path) -> None:
    """An absolute path outside the workspace is dropped, not stored verbatim."""
    with _index(workspace) as idx:
        idx.invalidate(["/definitely/outside/the/workspace/mod.py"])
        # next query flushes dirty; the outside path must not create a bogus row
        assert idx.find_symbol("alpha")
        rows = idx._conn.execute(  # noqa: SLF001 — white-box
            "SELECT COUNT(*) AS c FROM files WHERE path = ?",
            ("/definitely/outside/the/workspace/mod.py",),
        ).fetchone()
        assert rows["c"] == 0


def test_invalidate_reflects_removed_symbol(workspace: Path) -> None:
    mod = workspace / "pkg" / "mod.py"
    with _index(workspace) as idx:
        assert idx.find_symbol("beta")
        mod.write_text("def alpha():\n    return 1\n", encoding="utf-8")  # beta removed
        idx.invalidate(["pkg/mod.py"])
        assert idx.find_symbol("beta") == []
        assert idx.find_symbol("alpha")  # alpha still present


# ---------------------------------------------------------------------------
# Freshness path B — query-time refresh for external edits (no hook)
# ---------------------------------------------------------------------------


def test_query_time_refresh_updates_signature(workspace: Path) -> None:
    mod = workspace / "pkg" / "mod.py"
    with _index(workspace) as idx:
        before = idx.find_symbol("alpha")[0].signature
        assert before == "def alpha():"
        # external edit: same name, new signature, NO invalidate call
        mod.write_text("def alpha(x, y):\n    return x + y\n", encoding="utf-8")
        _bump_mtime(mod)
        after = idx.find_symbol("alpha")[0].signature
        assert after == "def alpha(x, y):"


def test_query_time_refresh_drops_deleted_file(workspace: Path) -> None:
    mod = workspace / "pkg" / "mod.py"
    with _index(workspace) as idx:
        assert idx.find_symbol("alpha")
        mod.unlink()  # file deleted externally
        _bump_mtime_dir(workspace / "pkg")
        # alpha was defined in mod.py; querying it triggers refresh → row dropped
        assert idx.find_symbol("alpha") == []


# ---------------------------------------------------------------------------
# SQLite reuse / persistence / incremental
# ---------------------------------------------------------------------------


def test_db_reuse_persists_across_reopen(workspace: Path) -> None:
    db = workspace / ".krodo" / "index" / "symbols.db"
    with _index(workspace) as idx:
        assert idx.find_symbol("alpha")
    # reopen without build_full — data must already be on disk
    with TreeSitterSymbolIndex(db, workspace, ignore=KrodoIgnore(workspace)) as idx2:
        assert idx2.find_symbol("alpha")
        assert idx2.stats().symbols >= 2


def test_second_build_is_incremental_noop(workspace: Path) -> None:
    with _index(workspace) as idx:
        before = idx.stats()
        # No file changes → reconcile must skip re-parsing (still correct).
        after = idx.build_full()
        assert after.files_indexed == before.files_indexed
        assert after.symbols == before.symbols


def test_build_picks_up_new_file(workspace: Path) -> None:
    with _index(workspace) as idx:
        (workspace / "extra.py").write_text("def extra():\n    pass\n", encoding="utf-8")
        idx.build_full()
        assert idx.find_symbol("extra")


def test_build_prunes_deleted_file(workspace: Path) -> None:
    with _index(workspace) as idx:
        assert idx.find_symbol("beta")
        (workspace / "pkg" / "mod.py").unlink()
        idx.build_full()
        assert idx.find_symbol("beta") == []  # pruned


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_satisfies_symbol_backend_protocol(workspace: Path) -> None:
    from krodo.indexer.base import SymbolBackend

    with _index(workspace) as idx:
        assert isinstance(idx, SymbolBackend)


# ---------------------------------------------------------------------------
# No-ignore mode + lazy sha256 confirm (review D2) + dirty-stat failure
# ---------------------------------------------------------------------------


def test_build_without_ignore_indexes_everything(tmp_path: Path) -> None:
    """When no KrodoIgnore is supplied, nothing is pruned (ignore is None)."""
    (tmp_path / "a.py").write_text("def alpha():\n    pass\n", encoding="utf-8")
    idx = TreeSitterSymbolIndex(tmp_path / ".krodo" / "index" / "symbols.db", tmp_path)
    idx.build_full()
    try:
        assert idx.find_symbol("alpha")
    finally:
        idx.close()


def test_dirty_path_deleted_before_query_is_dropped(workspace: Path) -> None:
    """invalidate() then file removal → next query drops the file (stat fails)."""
    with _index(workspace) as idx:
        assert idx.find_symbol("alpha")
        (workspace / "pkg" / "mod.py").unlink()
        idx.invalidate(["pkg/mod.py"])
        # query flushes dirty → stat fails → row deleted → alpha gone
        assert idx.find_symbol("alpha") == []


def test_lazy_sha256_confirms_unchanged_touch(workspace: Path) -> None:
    """Same-size mtime change with a cached sha256 short-circuits re-parse.

    Populates files.sha256 (which build_full never does), then touches the file
    without changing content. The freshness check confirms via sha256 and only
    bumps mtime — exercising the lazy-hash path (review D2).
    """
    import hashlib

    mod = workspace / "pkg" / "mod.py"
    with _index(workspace) as idx:
        sig_before = idx.find_symbol("alpha")[0].signature
        # cache the content hash as build_full would in a future revision
        digest = hashlib.sha256(mod.read_bytes()).hexdigest()
        idx._conn.execute(  # noqa: SLF001 — white-box: seed the lazy-hash column
            "UPDATE files SET sha256 = ? WHERE path = ?", (digest, "pkg/mod.py")
        )
        idx._conn.commit()
        # external touch: same size, new mtime, no content change, no invalidate
        _bump_mtime(mod)
        idx.find_symbol("alpha")
        # mtime column updated to the new touch time
        row = idx._conn.execute(  # noqa: SLF001
            "SELECT mtime FROM files WHERE path = ?", ("pkg/mod.py",)
        ).fetchone()
        assert row["mtime"] == mod.stat().st_mtime
        # content unchanged → signature identical (no re-parse drift)
        assert idx.find_symbol("alpha")[0].signature == sig_before


def test_wrong_sha256_forces_reextract(workspace: Path) -> None:
    """A stale/corrupt cached sha256 must NOT short-circuit — re-parse instead."""
    mod = workspace / "pkg" / "mod.py"
    with _index(workspace) as idx:
        # seed a bogus hash so the confirm check fails
        idx._conn.execute(  # noqa: SLF001
            "UPDATE files SET sha256 = ? WHERE path = ?", ("deadbeef", "pkg/mod.py")
        )
        idx._conn.commit()
        mod.write_text("def alpha(x, y):\n    return x + y\n", encoding="utf-8")
        _bump_mtime(mod)
        sig = idx.find_symbol("alpha")[0].signature
        assert sig == "def alpha(x, y):"  # re-extracted despite cached hash


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bump_mtime(path: Path) -> None:
    """Force a distinct mtime so the freshness check sees a change.

    Sleep *before* ``utime`` so the new mtime lands on a later tick than the
    mtime captured at build time (avoids flakes on coarse-grained filesystems).
    """
    time.sleep(0.01)
    os.utime(path, None)


def _bump_mtime_dir(path: Path) -> None:
    os.utime(path, None)


# ---------------------------------------------------------------------------
# IterableSymbolBackend — enumeration order + version counter (M10 PR1)
# ---------------------------------------------------------------------------


def test_iter_symbols_ordered_by_path_line_name(workspace: Path) -> None:
    """Enumeration must follow the ORDER BY contract (repo-map determinism)."""
    pkg = workspace / "pkg"
    pkg.mkdir(exist_ok=True)
    # mod.py: alpha at line 1, zeta at line 4 (line order within a file).
    (pkg / "mod.py").write_text(
        "def alpha():\n    pass\n\n\ndef zeta():\n    pass\n", encoding="utf-8"
    )
    # app.py sorts before mod.py by path.
    (pkg / "app.py").write_text("def beta():\n    pass\n", encoding="utf-8")
    with _index(workspace) as idx:
        syms = list(idx.iter_symbols())
        # Expect ORDER BY path, line, name: app.py beta, mod.py alpha, mod.py zeta
        assert [(s.path, s.line, s.name) for s in syms] == [
            ("pkg/app.py", 1, "beta"),
            ("pkg/mod.py", 1, "alpha"),
            ("pkg/mod.py", 5, "zeta"),
        ]


def test_iter_refs_ordered(workspace: Path) -> None:
    (workspace / "pkg").mkdir(exist_ok=True)
    (workspace / "pkg" / "mod.py").write_text(
        "def alpha():\n    return 1\ndef beta():\n    return alpha()\n",
        encoding="utf-8",
    )
    (workspace / "pkg" / "app.py").write_text("alpha()\nbeta()\n", encoding="utf-8")
    with _index(workspace) as idx:
        refs = list(idx.iter_refs())
        # ORDER BY path, line, name
        assert refs == sorted(refs, key=lambda r: (r.path, r.line, r.name))


def test_version_bumps_on_build(workspace: Path) -> None:
    with _index(workspace) as idx:
        assert idx.version > 0  # build_full stored files → bumped


def test_version_stable_on_noop_rebuild(workspace: Path) -> None:
    with _index(workspace) as idx:
        v = idx.version
        idx.build_full()  # nothing changed → no _store_file → no bump
        assert idx.version == v


def test_version_bumps_on_invalidate_without_query(workspace: Path) -> None:
    """invalidate bumps version optimistically even before any query flushes
    (so repo-map refresh detects a write on the very next turn)."""
    with _index(workspace) as idx:
        v = idx.version
        idx.invalidate(["pkg/mod.py"])
        assert idx.version > v  # bumped by invalidate alone


def test_version_bumps_on_flush_then_stable(workspace: Path) -> None:
    with _index(workspace) as idx:
        idx.invalidate(["pkg/mod.py"])
        v_after_invalidate = idx.version
        list(idx.iter_symbols())  # iter flushes dirty → _store_file bumps
        assert idx.version > v_after_invalidate
        stable = idx.version
        list(idx.iter_symbols())  # nothing dirty now → stable
        assert idx.version == stable


def test_satisfies_iterable_symbol_backend_protocol(workspace: Path) -> None:
    from krodo.indexer.base import IterableSymbolBackend

    with _index(workspace) as idx:
        assert isinstance(idx, IterableSymbolBackend)
