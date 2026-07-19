"""Tests for krodo.memory.repo_map — graph build, PageRank, render (M10 PR1).

Uses a fake ``IterableSymbolBackend`` (no SQLite) so the rendering pipeline is
exercised in isolation. Determinism and budget are the two invariants that
matter most (prompt-cache prerequisites).
"""

from __future__ import annotations

from collections.abc import Iterator

from krodo.indexer.base import SymbolDef, SymbolRef
from krodo.memory.repo_map import build_graph, pagerank, render_map

# --------------------------------------------------------------------------
# Fake backend (honours the ORDER BY contract; data pre-sorted)
# --------------------------------------------------------------------------


class _FakeBackend:
    """Minimal IterableSymbolBackend over canned, pre-sorted lists."""

    def __init__(self, symbols: list[SymbolDef], refs: list[SymbolRef], version: int = 0) -> None:
        self._symbols = symbols
        self._refs = refs
        self._version = version

    def iter_symbols(self) -> Iterator[SymbolDef]:
        return iter(self._symbols)

    def iter_refs(self) -> Iterator[SymbolRef]:
        return iter(self._refs)

    @property
    def version(self) -> int:
        return self._version


def _sym(
    path: str, line: int, name: str, kind: str = "function", sig: str | None = None
) -> SymbolDef:
    return SymbolDef(
        path=path,
        line=line,
        name=name,
        kind=kind,
        signature=sig if sig is not None else f"def {name}():",
    )


def _ref(path: str, line: int, name: str) -> SymbolRef:
    return SymbolRef(path=path, line=line, name=name)


# A small canned graph:
#   a.py defines alpha; b.py defines beta; c.py calls alpha+beta+len(builtin)
_CANNED_SYMS = [
    _sym("a.py", 1, "alpha"),
    _sym("b.py", 1, "beta"),
    _sym("c.py", 1, "gamma"),
    _sym("c.py", 5, "delta"),
]
_CANNED_REFS = [
    _ref("b.py", 2, "alpha"),  # b → a
    _ref("c.py", 2, "alpha"),  # c → a
    _ref("c.py", 3, "beta"),  # c → b
    _ref("c.py", 4, "len"),  # builtin, no def → dropped
]


def _backend() -> _FakeBackend:
    return _FakeBackend(list(_CANNED_SYMS), list(_CANNED_REFS))


# --------------------------------------------------------------------------
# build_graph
# --------------------------------------------------------------------------


def test_build_graph_edges_and_drops() -> None:
    out, nodes = build_graph(_backend())
    # b→a and c→a (so out[b][a] and out[c][a]); c→b; self/gamma/delta/len dropped
    assert out["b.py"]["a.py"] == 1.0
    assert out["c.py"]["a.py"] == 1.0
    assert out["c.py"]["b.py"] == 1.0
    assert "len" not in nodes  # name, not a node
    assert set(nodes) == {"a.py", "b.py", "c.py"}
    # c.py references itself? no self-loop even if gamma defined+called locally
    assert "c.py" not in out.get("c.py", {})


def test_build_graph_1_over_n_split() -> None:
    # "shared" defined in two files; one ref → split 0.5 / 0.5
    syms = [_sym("a.py", 1, "shared"), _sym("b.py", 1, "shared"), _sym("c.py", 1, "caller")]
    refs = [_ref("c.py", 2, "shared")]
    out, _ = build_graph(_FakeBackend(syms, refs))
    assert out["c.py"]["a.py"] == 0.5
    assert out["c.py"]["b.py"] == 0.5


def test_build_graph_self_loop_dropped() -> None:
    syms = [_sym("a.py", 1, "foo")]
    refs = [_ref("a.py", 2, "foo")]  # a references its own foo
    out, nodes = build_graph(_FakeBackend(syms, refs))
    assert out == {}  # the only edge was a self-loop
    assert nodes == []  # a.py only appeared as src of a dropped self-loop


# --------------------------------------------------------------------------
# pagerank
# --------------------------------------------------------------------------


def test_pagerank_deterministic() -> None:
    out, nodes = build_graph(_backend())
    r1 = pagerank(out, nodes)
    r2 = pagerank(out, nodes)
    assert r1 == r2  # fixed iterations + sorted nodes → byte-stable


def test_pagerank_referenced_file_ranks_higher() -> None:
    # a.py is referenced by b and c; b.py only by c; c.py by nobody.
    out, nodes = build_graph(_backend())
    rank = pagerank(out, nodes)
    assert rank["a.py"] > rank["c.py"]  # most-depended-on ranks highest
    assert rank["b.py"] > rank["c.py"]


def test_pagerank_empty() -> None:
    assert pagerank({}, []) == {}


# --------------------------------------------------------------------------
# render_map
# --------------------------------------------------------------------------


def _char_count(s: str) -> int:
    return len(s)


def test_render_map_deterministic() -> None:
    b = _backend()
    m1 = render_map(b, 2048, _char_count)
    m2 = render_map(_backend(), 2048, _char_count)
    assert m1 == m2  # byte-stable across calls/backends with same data


def test_render_map_empty_backend() -> None:
    assert render_map(_FakeBackend([], []), 2048, _char_count) == ""


def test_render_map_orders_by_rank() -> None:
    m = render_map(_backend(), 2048, _char_count)
    # a.py (most referenced) should appear before c.py (referenced by none)
    assert m.index("a.py") < m.index("c.py")


def test_render_map_budget_is_hard_cap() -> None:
    # Budget large enough for one file but not all — extra files skipped.
    b = _backend()
    full = render_map(b, 10_000, _char_count)
    assert _char_count(full) <= 10_000
    # Tight budget: still never exceeds the cap.
    tight = render_map(b, 60, _char_count)
    assert _char_count(tight) <= 60


def test_render_map_directory_grouping() -> None:
    syms = [
        _sym("pkg/mod.py", 1, "alpha"),
        _sym("pkg/mod.py", 5, "beta"),
        _sym("pkg/app.py", 1, "gamma"),
    ]
    refs = [_ref("pkg/app.py", 2, "alpha"), _ref("pkg/app.py", 3, "beta")]
    m = render_map(_FakeBackend(syms, refs), 2048, _char_count)
    # Directory header printed once, both files nested under it.
    assert "pkg/" in m
    assert m.index("pkg/") < m.index("mod.py") < m.index("app.py")


def test_render_map_symbols_ordered_by_refcount() -> None:
    # In a file with a referenced and an unreferenced symbol, the referenced
    # one (higher ref count) lists first regardless of source line order.
    syms = [
        _sym("x.py", 10, "unused"),  # defined first by line, but never called
        _sym("x.py", 1, "used"),  # called elsewhere → should list first
    ]
    refs = [_ref("other.py", 2, "used")]
    backend = _FakeBackend(syms + [_sym("other.py", 1, "other")], refs)
    m = render_map(backend, 2048, _char_count)
    x_block = m.split("x.py", 1)[1].split("other.py", 1)[0]
    assert x_block.index("used") < x_block.index("unused")


def test_build_graph_caps_ambiguous_names() -> None:
    """Names defined in >MAX_DEFS_PER_NAME files are dropped as noise (this is
    what keeps PageRank feasible on large repos — common names like ``get`` /
    ``init`` defined across dozens of files would explode the edge count)."""
    # 11 defs for "common" → capped (>10), the ref produces no edges.
    syms = [_sym(f"f{i}.py", 1, "common") for i in range(11)]
    syms.append(_sym("caller.py", 1, "caller"))
    out, nodes = build_graph(_FakeBackend(syms, [_ref("caller.py", 2, "common")]))
    assert out == {}  # capped → no edges at all

    # 10 defs for "shared" → kept (≤10), edges split 1/10 each (total weight 1.0).
    syms2 = [_sym(f"g{i}.py", 1, "shared") for i in range(10)]
    syms2.append(_sym("caller2.py", 1, "caller2"))
    out2, _ = build_graph(_FakeBackend(syms2, [_ref("caller2.py", 2, "shared")]))
    assert set(out2["caller2.py"]) == {f"g{i}.py" for i in range(10)}
    assert abs(sum(out2["caller2.py"].values()) - 1.0) < 1e-9


# --------------------------------------------------------------------------
# RepoMapManager — version-gated refresh (PR2①)
# --------------------------------------------------------------------------


class _MutableBackend:
    """IterableSymbolBackend whose version + data can be mutated between calls."""

    def __init__(self, symbols: list[SymbolDef], refs: list[SymbolRef]) -> None:
        self.symbols = symbols
        self.refs = refs
        self.version = 0

    def iter_symbols(self) -> Iterator[SymbolDef]:
        return iter(self.symbols)

    def iter_refs(self) -> Iterator[SymbolRef]:
        return iter(self.refs)


def test_manager_initial_render_records_version() -> None:
    from krodo.memory.repo_map import RepoMapManager

    backend = _MutableBackend([_sym("a.py", 1, "alpha")], [])
    backend.version = 5
    mgr = RepoMapManager(backend, 2048, _char_count)
    text = mgr.initial_render()
    assert "alpha" in text
    assert mgr.last_version == 5  # read after render


def test_manager_render_if_changed_skips_when_version_unchanged() -> None:
    from krodo.memory.repo_map import RepoMapManager

    backend = _MutableBackend([_sym("a.py", 1, "alpha")], [])
    mgr = RepoMapManager(backend, 2048, _char_count)
    mgr.initial_render()
    # No version change → None (no re-render, no history mutation).
    assert mgr.render_if_changed() is None


def test_manager_render_if_changed_none_when_bytes_identical() -> None:
    from krodo.memory.repo_map import RepoMapManager

    backend = _MutableBackend([_sym("a.py", 1, "alpha")], [])
    mgr = RepoMapManager(backend, 2048, _char_count)
    mgr.initial_render()
    backend.version += 1  # version changed...
    # ...but the data is identical → re-render produces same bytes → None
    assert mgr.render_if_changed() is None


def test_manager_render_if_changed_returns_new_text_when_data_changes() -> None:
    from krodo.memory.repo_map import RepoMapManager

    backend = _MutableBackend([_sym("a.py", 1, "alpha")], [])
    mgr = RepoMapManager(backend, 2048, _char_count)
    first = mgr.initial_render()
    backend.version += 1
    backend.symbols.append(_sym("b.py", 1, "beta"))  # data actually changed
    new = mgr.render_if_changed()
    assert new is not None
    assert new != first
    assert "beta" in new
