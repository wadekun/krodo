"""Single-file symbol extraction (tree-sitter).

Given a file's bytes, pick the grammar by extension, parse, run the Aider-style
tag query, and emit :class:`~krodo.indexer.base.SymbolDef` /
:class:`~krodo.indexer.base.SymbolRef` lists. Robust by construction (review L):

* files larger than ``_MAX_FILE_BYTES`` → empty (caller logs the skip);
* NUL byte in content → treated as binary → empty;
* unsupported extension / no grammar → empty;
* parse errors → tree-sitter is error-recovery, so partial trees still yield
  symbols; an unexpected exception degrades to empty rather than crashing a
  full-repo build.

Signature extraction is **method 1** (review B): read the definition line of
the symbol's identifier node. Multi-line headers get a trailing ``" …"``.
Isolated in :func:`_extract_signature` so M10 repo-map rendering can upgrade it
in one place without touching callers.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import NamedTuple

from tree_sitter import Node, Parser, Query, QueryCursor
from tree_sitter_language_pack import get_parser

from krodo.indexer.base import SymbolDef, SymbolRef

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_MAX_FILE_BYTES = 1 * 1024 * 1024  # 1 MB — anything bigger is skipped (review L)
_SIG_MAX_CHARS = 120  # cap a single signature line so map rendering stays compact
_DEFINITION_BLOCK_TERMINATORS = ("{", ":", ";")

# extension (lowercase, with dot) → tree-sitter grammar name
_GRAMMAR_BY_EXT: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
}

# grammar name → query file base name (``<base>-tags.scm`` in ``queries/``).
# ``tsx`` reuses the typescript query (TSX grammar is TS + JSX).
_QUERY_FOR_GRAMMAR: dict[str, str] = {
    "python": "python",
    "javascript": "javascript",
    "typescript": "typescript",
    "tsx": "typescript",
    "go": "go",
}

_QUERY_DIR = Path(__file__).parent / "queries"

_DEF_PREFIX = "name.definition."
_REF_PREFIX = "name.reference."

# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------


class FileExtraction(NamedTuple):
    """Defs + refs extracted from a single file (both possibly empty)."""

    defs: list[SymbolDef]
    refs: list[SymbolRef]


# ---------------------------------------------------------------------------
# Grammar / query caches (process-global; Parser and Query are reusable)
# ---------------------------------------------------------------------------

_parsers: dict[str, Parser] = {}
_queries: dict[str, Query] = {}


def _load_query_source(grammar: str) -> str:
    base = _QUERY_FOR_GRAMMAR[grammar]
    return (_QUERY_DIR / f"{base}-tags.scm").read_text(encoding="utf-8")


def _parser_and_query(grammar: str) -> tuple[Parser, Query]:
    """Return a cached (Parser, Query) for *grammar*, building on first use."""
    parser = _parsers.get(grammar)
    query = _queries.get(grammar)
    if parser is None or query is None:
        parser = get_parser(grammar)
        language = parser.language
        if language is None:  # pragma: no cover — grammar ships a Language object
            raise RuntimeError(f"tree-sitter grammar {grammar!r} exposed no Language")
        query = Query(language, _load_query_source(grammar))
        _parsers[grammar] = parser
        _queries[grammar] = query
    return parser, query


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def supported_extensions() -> frozenset[str]:
    """Return the set of indexed extensions (with leading dot, lowercase)."""
    return frozenset(_GRAMMAR_BY_EXT)


def grammar_for_path(path: str | Path) -> str | None:
    """Return the grammar name for *path*'s extension, or ``None`` if unsupported."""
    return _GRAMMAR_BY_EXT.get(Path(path).suffix.lower())


def extract_symbols(rel_path: str, source: bytes) -> FileExtraction:
    """Extract definitions and references from *source*.

    Parameters
    ----------
    rel_path:
        Workspace-relative POSIX path; stored verbatim on each symbol. Only the
        suffix is consulted to pick the grammar.
    source:
        Raw file bytes.

    Returns
    -------
    FileExtraction
        ``(defs, refs)`` — both empty when the file is skipped (unsupported /
        too large / binary / unparseable). Never raises.
    """
    grammar = grammar_for_path(rel_path)
    if grammar is None:
        return FileExtraction([], [])

    if len(source) > _MAX_FILE_BYTES:
        log.debug("indexer: skip oversized file %s (%d bytes)", rel_path, len(source))
        return FileExtraction([], [])

    # NUL byte → almost certainly binary (tree-sitter would still "parse" it
    # but the symbols are noise).
    if b"\x00" in source:
        log.debug("indexer: skip binary file %s (NUL byte)", rel_path)
        return FileExtraction([], [])

    try:
        parser, query = _parser_and_query(grammar)
        tree = parser.parse(source)
    except Exception:  # noqa: BLE001 — degrade to empty, never crash a build
        log.debug("indexer: parse failed for %s", rel_path, exc_info=True)
        return FileExtraction([], [])

    root = tree.root_node
    if root is None:  # pragma: no cover — defensive; parse() always returns a tree
        return FileExtraction([], [])

    try:
        captures: dict[str, list[Node]] = QueryCursor(query).captures(root)
    except Exception:  # noqa: BLE001 — query runtime error → empty
        log.debug("indexer: query failed for %s", rel_path, exc_info=True)
        return FileExtraction([], [])

    src_lines = source.decode("utf-8", errors="replace").splitlines()
    defs: list[SymbolDef] = []
    refs: list[SymbolRef] = []

    for cap_name, nodes in captures.items():
        if cap_name.startswith(_DEF_PREFIX):
            kind = cap_name[len(_DEF_PREFIX) :]
            for node in nodes:
                defs.append(_node_to_def(rel_path, src_lines, node, kind))
        elif cap_name.startswith(_REF_PREFIX):
            for node in nodes:
                name = _node_text(node)
                if name:
                    refs.append(SymbolRef(path=rel_path, line=node.start_point.row + 1, name=name))

    return FileExtraction(defs, refs)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _node_text(node: Node) -> str:
    raw = node.text
    if raw is None:  # pragma: no cover — Node.text is bytes in 0.26
        return ""
    return raw.decode("utf-8", errors="replace")


def _node_to_def(rel_path: str, src_lines: list[str], node: Node, kind: str) -> SymbolDef:
    return SymbolDef(
        path=rel_path,
        line=node.start_point.row + 1,
        name=_node_text(node),
        kind=kind,
        signature=_extract_signature(src_lines, node),
    )


def _extract_signature(src_lines: list[str], name_node: Node) -> str:
    """Return the definition line of *name_node*, marking multi-line headers.

    Uses the identifier's parent node span to detect whether the header spills
    onto the next line; if so (and the line lacks a block opener) a trailing
    ``" …"`` is appended so M10 repo-map rendering can signal truncation.
    """
    row = name_node.start_point.row
    if row >= len(src_lines):
        return ""
    line = src_lines[row].strip()

    parent = name_node.parent
    end_row = parent.end_point.row if parent is not None else row
    header_continues = end_row > row and not line.endswith(_DEFINITION_BLOCK_TERMINATORS)
    if header_continues:
        line = f"{line} …"

    if len(line) > _SIG_MAX_CHARS:
        line = f"{line[: _SIG_MAX_CHARS - 1]}…"
    return line
