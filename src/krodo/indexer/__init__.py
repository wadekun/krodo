"""indexer — tree-sitter symbol index (Phase 2 M9 data foundation).

This package is the data layer that M10 (repo-map) and M11 (symbol tools)
consume. It exposes a small Protocol (:class:`SymbolBackend`) and one concrete
implementation (:class:`TreeSitterSymbolIndex`).

Importing this package has no side effects; databases are only opened when a
:class:`TreeSitterSymbolIndex` is instantiated.
"""

from __future__ import annotations

from krodo.indexer.base import (
    IndexStats,
    IterableSymbolBackend,
    SymbolBackend,
    SymbolDef,
    SymbolRef,
)
from krodo.indexer.extract import extract_symbols, grammar_for_path, supported_extensions
from krodo.indexer.symbol_index import TreeSitterSymbolIndex

__all__ = [
    "IndexStats",
    "IterableSymbolBackend",
    "SymbolBackend",
    "SymbolDef",
    "SymbolRef",
    "TreeSitterSymbolIndex",
    "extract_symbols",
    "grammar_for_path",
    "supported_extensions",
]
