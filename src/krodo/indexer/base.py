"""Symbol index Protocol + value objects (architecture.md §5 — M9 data foundation).

This module is the **single contract** that M10 (repo-map) and M11 (symbol
tools) consume. M9 ships one concrete implementation
(:class:`~krodo.indexer.symbol_index.TreeSitterSymbolIndex`, ``backend="treesitter"``);
Phase 3 will add an LSP backend (``backend="lsp"``, ``precision="semantic"``)
without touching this Protocol or its dataclasses.

Design notes (see ``docs/reviews/m9_plan_review.md`` — review A):

* ``backend`` records *which* backend produced a symbol on a Composite chain
  (so callers can tell a tree-sitter hit from an LSP hit at query time).
* ``precision`` is ``"syntactic"`` for tree-sitter (node-text based) and will be
  ``"semantic"`` for LSP (resolved via the language server). M9 always emits
  ``"syntactic"`` — zero runtime cost, but the field is reserved now so that
  adding the LSP backend is non-breaking.
* All ``path`` values are **relative to the workspace root** (POSIX-style) so
  that the index is portable across machines and survives a workspace move.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

Precision = Literal["syntactic", "semantic"]


@dataclass(frozen=True)
class SymbolDef:
    """A symbol definition (function / class / method / …).

    Attributes
    ----------
    path:
        Workspace-relative POSIX path of the defining file.
    line:
        1-based line of the symbol's *name*.
    name:
        Bare symbol name (no qualifiers).
    kind:
        Aider-style kind: ``function`` / ``class`` / ``method`` / ``constant``
        / ``type`` / ``module`` / ``variable``.
    signature:
        Best-effort definition-line snippet for repo-map rendering. May be
        truncated (trailing ``…``) for multi-line signatures.
    backend:
        Source backend tag (``"treesitter"`` in M9).
    precision:
        ``"syntactic"`` (tree-sitter) or ``"semantic"`` (LSP, Phase 3).
    """

    path: str
    line: int
    name: str
    kind: str
    signature: str
    backend: str = "treesitter"
    precision: Precision = "syntactic"


@dataclass(frozen=True)
class SymbolRef:
    """A reference to a symbol (call site / type use / import)."""

    path: str
    line: int
    name: str
    backend: str = "treesitter"
    precision: Precision = "syntactic"


@dataclass(frozen=True)
class IndexStats:
    """Read-only snapshot of the index, surfaced via ``doctor`` and events."""

    backend: str
    files_indexed: int
    symbols: int
    references: int
    build_ms: int
    db_path: str | None = None


@runtime_checkable
class SymbolBackend(Protocol):
    """Read/query interface over a symbol index.

    Implementations must be cheap to query (<100ms on a name-indexed SQLite
    table) and tolerant of stale state: a query that hits a file whose mtime
    has changed since the last ``build_full`` must re-extract that single file
    on the fly rather than trigger a full rebuild.
    """

    def find_symbol(self, name: str) -> list[SymbolDef]:
        """Return all definitions whose name matches *name* (exact match)."""
        ...

    def find_references(self, name: str) -> list[SymbolRef]:
        """Return all references whose name matches *name* (exact match)."""
        ...

    def stats(self) -> IndexStats:
        """Return a snapshot of indexed file / symbol / reference counts."""
        ...

    def invalidate(self, paths: Iterable[str | Path]) -> None:
        """Drop index rows for the given workspace-relative *paths*.

        Called by write tools (``write_file`` / ``edit_file`` / ``apply_patch``)
        after a successful write. The next query touching those paths triggers
        a lazy single-file re-extract.
        """
        ...

    def close(self) -> None:
        """Release any resources held by this backend (e.g. the DB connection).

        Called once when the session/REPL loop that owns this backend ends
        (headless exit, REPL exit, and before rebuilding components on
        ``:resume``). Idempotent implementations are encouraged but not
        required — callers only call this once per instance.
        """
        ...


@runtime_checkable
class IterableSymbolBackend(Protocol):
    """Bulk-enumeration interface consumed by M10 repo-map.

    Deliberately distinct from :class:`SymbolBackend` (which is query-shaped):
    full-index enumeration is a tree-sitter-specific affordance. A future LSP
    backend can satisfy ``SymbolBackend`` queries cheaply but cannot enumerate
    the whole workspace without walking every file, so enumeration stays off
    the query Protocol. ``memory.repo_map`` depends on this Protocol (not the
    concrete ``TreeSitterSymbolIndex``) so it remains testable with a fake
    backend (engineering rule #2 — Protocol first).

    Implementations MUST enumerate in a **deterministic order**
    (``ORDER BY path, line, name``). Repo-map output is byte-stable across
    renders — a prompt-cache invariant — and any order drift propagates
    through graph construction into the PageRank float-summation order and
    thence into the rendered text.
    """

    def iter_symbols(self) -> Iterator[SymbolDef]:
        """Yield every indexed definition, ordered by (path, line, name)."""
        ...

    def iter_refs(self) -> Iterator[SymbolRef]:
        """Yield every indexed reference, ordered by (path, line, name)."""
        ...

    @property
    def version(self) -> int:
        """Monotonic counter bumped whenever index content may have changed
        (build / re-extract / delete / invalidate). Readers compare it to
        detect "no change since last render" without re-reading rows; a bump
        is optimistic (may fire even when the re-extract is byte-identical),
        so callers that need cache stability must still byte-compare.
        """
        ...
