"""Tree-sitter-backed symbol index stored in SQLite (architecture.md §5 — M9).

:class:`TreeSitterSymbolIndex` is the single M9 implementation of the
:class:`~krodo.indexer.base.SymbolBackend` Protocol. It:

* parses every indexable file once (via :mod:`krodo.indexer.extract`) and stores
  def/ref rows keyed by **workspace-relative POSIX path** (portable across
  machines; review D3);
* keeps a SQLite database at ``<workspace>/.krodo/index/symbols.db`` in **WAL**
  mode so reads never block writes and query latency stays under the 100ms
  budget (review D1);
* validates freshness with **mtime + size** as the primary signal and reserves
  ``sha256`` for the rare "touched but same size" case — computed lazily, never
  during a full build (review D2);
* on query, re-extracts **only the files whose rows were hit** when their
  mtime/size changed — never a full scan or rebuild on the query path
  (review D4).
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import time
from collections.abc import Iterable, Iterator
from pathlib import Path

from krodo.indexer.base import IndexStats, SymbolDef, SymbolRef
from krodo.indexer.extract import extract_symbols, supported_extensions

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path   TEXT PRIMARY KEY,
    mtime  REAL NOT NULL,
    size   INTEGER NOT NULL,
    sha256 TEXT
);
CREATE TABLE IF NOT EXISTS symbols (
    path      TEXT NOT NULL,
    line      INTEGER NOT NULL,
    name      TEXT NOT NULL,
    kind      TEXT NOT NULL,
    signature TEXT NOT NULL,
    FOREIGN KEY(path) REFERENCES files(path) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_path ON symbols(path);
CREATE TABLE IF NOT EXISTS refs (
    path TEXT NOT NULL,
    line INTEGER NOT NULL,
    name TEXT NOT NULL,
    FOREIGN KEY(path) REFERENCES files(path) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_refs_name ON refs(name);
CREATE INDEX IF NOT EXISTS idx_refs_path ON refs(path);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class TreeSitterSymbolIndex:
    """SQLite-backed symbol index implementing ``SymbolBackend``."""

    backend = "treesitter"

    def __init__(
        self,
        db_path: Path,
        workspace_root: Path,
        ignore: object | None = None,
    ) -> None:
        self._db_path = db_path
        self._root = workspace_root
        # ``ignore`` is typed loosely to avoid a hard import dep here; the
        # concrete type is KrodoIgnore with an ``is_ignored(Path) -> bool``.
        self._ignore = ignore
        # Paths dirtied by write-tool hooks (invalidate) since the last query.
        # Re-extracted lazily on the next query (see ``_flush_dirty``). In-memory
        # only: a fresh process always runs ``build_full`` at startup, so there
        # is no cross-process staleness window.
        self._dirty: set[str] = set()
        # Monotonic content-revision counter (M10 repo-map version gate).
        # Bumped on invalidate (optimistic) and whenever rows are written or
        # deleted (_store_file / _delete_file); stable across no-op builds so
        # repo-map can skip a re-render by comparing this alone.
        self._version: int = 0
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        # WAL is persistent (stored in the db header); foreign_keys is
        # per-connection. synchronous=NORMAL is the recommended WAL tradeoff.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> TreeSitterSymbolIndex:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # IterableSymbolBackend — bulk enumeration + version (M10 repo-map)
    # ------------------------------------------------------------------

    @property
    def version(self) -> int:
        return self._version

    def iter_symbols(self) -> Iterator[SymbolDef]:
        """Yield every definition, ordered by (path, line, name).

        Flushes pending invalidations first so the enumeration reflects writes
        from the current turn. Ordering is a hard contract (see
        :class:`~krodo.indexer.base.IterableSymbolBackend`) — repo-map
        byte-stability depends on it.
        """
        self._flush_dirty()
        rows = self._conn.execute(
            "SELECT path, line, name, kind, signature FROM symbols ORDER BY path, line, name"
        )
        for r in rows:
            yield SymbolDef(
                path=r["path"],
                line=r["line"],
                name=r["name"],
                kind=r["kind"],
                signature=r["signature"],
                backend=self.backend,
            )

    def iter_refs(self) -> Iterator[SymbolRef]:
        """Yield every reference, ordered by (path, line, name)."""
        self._flush_dirty()
        rows = self._conn.execute("SELECT path, line, name FROM refs ORDER BY path, line, name")
        for r in rows:
            yield SymbolRef(path=r["path"], line=r["line"], name=r["name"], backend=self.backend)

    # ------------------------------------------------------------------
    # SymbolBackend — queries
    # ------------------------------------------------------------------

    def find_symbol(self, name: str) -> list[SymbolDef]:
        self._flush_dirty()
        hit_paths = self._hit_paths("symbols", name)
        if hit_paths:
            self._refresh_if_stale(hit_paths)
        rows = self._conn.execute(
            "SELECT path, line, name, kind, signature FROM symbols WHERE name = ?",
            (name,),
        ).fetchall()
        return [
            SymbolDef(
                path=r["path"],
                line=r["line"],
                name=r["name"],
                kind=r["kind"],
                signature=r["signature"],
                backend=self.backend,
            )
            for r in rows
        ]

    def find_references(self, name: str) -> list[SymbolRef]:
        self._flush_dirty()
        hit_paths = self._hit_paths("refs", name)
        if hit_paths:
            self._refresh_if_stale(hit_paths)
        rows = self._conn.execute(
            "SELECT path, line, name FROM refs WHERE name = ?",
            (name,),
        ).fetchall()
        return [
            SymbolRef(path=r["path"], line=r["line"], name=r["name"], backend=self.backend)
            for r in rows
        ]

    def stats(self) -> IndexStats:
        return IndexStats(
            backend=self.backend,
            files_indexed=self._count("files"),
            symbols=self._count("symbols"),
            references=self._count("refs"),
            build_ms=int(self._meta("build_ms", "0")),
            db_path=str(self._db_path),
        )

    # ------------------------------------------------------------------
    # SymbolBackend — mutation
    # ------------------------------------------------------------------

    def invalidate(self, paths: Iterable[str | Path]) -> None:
        """Mark *paths* for lazy re-extraction on the next query.

        Called by write tools after a successful write. We do NOT delete rows
        here (that would make the file invisible until the next ``build_full``);
        instead we record the path as dirty and let ``_flush_dirty`` re-parse it
        at the start of the next query, so newly-added symbols are discoverable
        immediately (acceptance: edit a function name → next query reflects it).

        Paths are normalized to workspace-relative POSIX strings. Absolute paths
        inside the workspace are made relative; absolute paths *outside* the
        workspace are dropped (debug log) — otherwise ``self._root / abs_path``
        silently replaces the left operand and the same file ends up indexed
        under both relative and absolute keys (duplicate, silently corrupted
        query results).
        """
        for path in paths:
            rel = self._normalize_rel(path)
            if rel is not None:
                self._dirty.add(rel)
                # Optimistic version bump: a write may have changed content, so
                # the next repo-map refresh must re-render even if no query has
                # flushed the dirty file yet (otherwise the map lags a turn).
                # The render's byte-compare absorbs "write didn't change map".
                self._version += 1

    # ------------------------------------------------------------------
    # Build / reconcile
    # ------------------------------------------------------------------

    def build_full(self) -> IndexStats:
        """Reconcile the index against the current workspace tree.

        Inserts new files, re-extracts files whose mtime/size changed, and
        prunes rows for files that no longer exist. Idempotent: a second call
        with no changes is a no-op (only ``stat`` calls, no re-parse).
        """
        start = time.monotonic()
        seen: set[str] = set()
        for rel_path in self._iter_indexable_files():
            seen.add(rel_path)
            self._reconcile_file(rel_path)
        self._prune_missing(seen)
        build_ms = int((time.monotonic() - start) * 1000)
        self._set_meta("build_ms", str(build_ms))
        self._conn.commit()
        return self.stats()

    # ------------------------------------------------------------------
    # Internals — file iteration
    # ------------------------------------------------------------------

    def _is_ignored(self, rel: Path) -> bool:
        ignore = self._ignore
        if ignore is None:
            return False
        try:
            return bool(ignore.is_ignored(rel))  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — never let ignore errors abort a build
            return False

    def _normalize_rel(self, path: str | Path) -> str | None:
        """Return *path* as a workspace-relative POSIX string, or ``None``.

        Absolute paths inside the workspace are made relative; absolute paths
        outside the workspace are dropped with a debug log. Relative paths are
        passed through as POSIX (callers — ``_iter_indexable_files`` and the PR2
        write hooks — supply clean workspace-relative or absolute-under-root
        paths).
        """
        p = Path(path)
        if p.is_absolute():
            try:
                p = p.relative_to(self._root)
            except ValueError:
                log.debug("indexer: dropping path outside workspace: %s", path)
                return None
        return p.as_posix()

    def _iter_indexable_files(self) -> set[str]:
        """Return the set of workspace-relative POSIX paths to index.

        Uses ``os.walk`` and prunes ignored directories in-place so we never
        descend into ``node_modules/`` etc. (avoids stat'ing 100k vendored
        files). Non-git repos are supported — there is no gitpython dependency.
        """
        root = self._root
        exts = supported_extensions()
        out: set[str] = set()
        for dirpath, dirnames, filenames in os.walk(root):
            rel_dir = Path(dirpath).relative_to(root)
            # prune ignored subdirs in place (don't descend)
            dirnames[:] = [d for d in dirnames if not self._is_ignored(rel_dir / d)]
            for fn in filenames:
                rel = rel_dir / fn
                if self._is_ignored(rel):
                    continue
                if rel.suffix.lower() in exts:
                    out.add(rel.as_posix())
        return out

    # ------------------------------------------------------------------
    # Internals — per-file reconcile / freshness
    # ------------------------------------------------------------------

    def _reconcile_file(self, rel_path: str) -> None:
        full = self._root / rel_path
        try:
            st = full.stat()
        except OSError:
            log.debug("indexer: stat failed for %s", rel_path, exc_info=True)
            return
        stored = self._conn.execute(
            "SELECT mtime, size FROM files WHERE path = ?", (rel_path,)
        ).fetchone()
        if stored is not None:
            if stored["mtime"] == st.st_mtime and stored["size"] == st.st_size:
                return  # unchanged — skip re-parse (the common incremental case)
        self._reindex_file(rel_path, st)

    def _reindex_file(self, rel_path: str, st: object) -> None:
        full = self._root / rel_path
        try:
            source = full.read_bytes()
        except OSError:
            log.debug("indexer: read failed for %s", rel_path, exc_info=True)
            self._delete_file(rel_path)
            return
        extraction = extract_symbols(rel_path, source)
        self._store_file(rel_path, st, extraction.defs, extraction.refs)

    def _store_file(
        self,
        rel_path: str,
        st: object,
        defs: list[SymbolDef],
        refs: list[SymbolRef],
    ) -> None:
        cur = self._conn
        # Replace any prior rows for this file (FK cascade clears symbols/refs).
        cur.execute("DELETE FROM files WHERE path = ?", (rel_path,))
        cur.execute(
            "INSERT INTO files (path, mtime, size) VALUES (?, ?, ?)",
            (rel_path, st.st_mtime, st.st_size),  # type: ignore[attr-defined]
        )
        if defs:
            cur.executemany(
                "INSERT INTO symbols (path, line, name, kind, signature) VALUES (?, ?, ?, ?, ?)",
                [(rel_path, d.line, d.name, d.kind, d.signature) for d in defs],
            )
        if refs:
            cur.executemany(
                "INSERT INTO refs (path, line, name) VALUES (?, ?, ?)",
                [(rel_path, r.line, r.name) for r in refs],
            )
        self._version += 1  # content written → repo-map must re-render

    def _delete_file(self, rel_path: str) -> None:
        self._conn.execute("DELETE FROM files WHERE path = ?", (rel_path,))
        self._version += 1  # content removed → repo-map must re-render

    def _prune_missing(self, seen: set[str]) -> None:
        rows = self._conn.execute("SELECT path FROM files").fetchall()
        gone = [r["path"] for r in rows if r["path"] not in seen]
        for path in gone:
            self._delete_file(path)

    # ------------------------------------------------------------------
    # Internals — query-time freshness (review D2 + D4)
    # ------------------------------------------------------------------

    def _flush_dirty(self) -> None:
        """Re-extract every path dirtied by a write-tool hook since last query.

        This is the primary invalidation path: it makes new symbols immediately
        discoverable after ``edit_file`` / ``apply_patch``. ``_refresh_if_stale``
        (below) is the secondary path that catches external edits / ``run_shell``
        writes for which no hook fired — it only re-parses files already hit by
        the current query.
        """
        if not self._dirty:
            return
        dirty = sorted(self._dirty)
        self._dirty.clear()
        for path in dirty:
            full = self._root / path
            try:
                st = full.stat()
            except OSError:
                self._delete_file(path)
                continue
            self._reindex_file(path, st)
        self._conn.commit()

    def _hit_paths(self, table: str, name: str) -> set[str]:
        rows = self._conn.execute(
            f"SELECT DISTINCT path FROM {table} WHERE name = ?",  # noqa: S608
            (name,),
        ).fetchall()
        return {r["path"] for r in rows}

    def _refresh_if_stale(self, paths: set[str]) -> None:
        """Re-extract only the hit files whose mtime/size changed (review D4).

        For the rare "same size, different mtime" case (e.g. ``touch``) we
        confirm via sha256 before re-parsing, and cache the hash so later
        touches short-circuit (review D2). sha256 is never computed during a
        full build — only here, on a single queried file.
        """
        changed = False
        for path in paths:
            row = self._conn.execute(
                "SELECT mtime, size, sha256 FROM files WHERE path = ?", (path,)
            ).fetchone()
            if row is None:
                continue
            full = self._root / path
            try:
                st = full.stat()
            except OSError:
                self._delete_file(path)
                changed = True
                continue
            if row["mtime"] == st.st_mtime and row["size"] == st.st_size:
                continue  # fresh
            if row["size"] == st.st_size and row["sha256"]:
                # suspicious touch — confirm content before re-parsing
                if _sha256(full) == row["sha256"]:
                    self._conn.execute(
                        "UPDATE files SET mtime = ? WHERE path = ?",
                        (st.st_mtime, path),
                    )
                    changed = True
                    continue
            self._reindex_file(path, st)
            changed = True
        if changed:
            self._conn.commit()

    # ------------------------------------------------------------------
    # Internals — meta + counts
    # ------------------------------------------------------------------

    def _count(self, table: str) -> int:
        row = self._conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()  # noqa: S608
        return int(row["c"]) if row is not None else 0

    def _meta(self, key: str, default: str) -> str:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row is not None else default

    def _set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
