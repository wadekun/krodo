"""Aider-style repo-map over the M9 symbol index (Phase 2 M10).

Builds a file↔file reference graph from the index's ``refs``/``symbols`` tables,
ranks files with a handwritten PageRank (no networkx), and renders a compact
signature tree of the top files into a token budget. The rendered text is
injected as a ``<repo_map>`` context message by the CLI (PR2).

**Determinism is a hard invariant** (a prompt-cache prerequisite): the same
index must render byte-identical text across calls. It rests on three things —

* :func:`IterableSymbolBackend.iter_symbols` / ``iter_refs`` enumerate in
  ``ORDER BY path, line, name`` order (the contract on the Protocol);
* :func:`build_graph` preserves that order (dict insertion order is stable);
* :func:`pagerank` uses **fixed** iteration count (not a convergence threshold,
  whose iteration count would vary) and iterates sorted node lists, so float
  summation order is deterministic.

**Approximate graph.** Edges are resolved by *bare symbol name*: a reference to
``foo`` contributes edges to every file that *defines* ``foo``, split equally
(1/n). There is no type / namespace resolution (``a.foo`` vs ``foo``), no
definition is found for builtins/externals (``len``, ``print`` → dropped), and
self-loops are discarded. This is the same name-based approximation Aider uses;
it is good enough for *ranking* but is not a precise call graph.

PageRank: edges point referrer → definition, so a file that many files depend on
accumulates rank (the "important" files surface to the top).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from pathlib import PurePosixPath

from krodo.indexer.base import IterableSymbolBackend, SymbolDef

_DAMPING = 0.85
_ITERATIONS = 30  # fixed (not convergence-based) → deterministic output
# A bare name defined in more than this many files is treated as unresolvable
# noise (no meaningful edge) — keeps the graph sparse on large repos.
_MAX_DEFS_PER_NAME = 10

# --------------------------------------------------------------------------
# Graph construction
# --------------------------------------------------------------------------


def build_graph(
    backend: IterableSymbolBackend,
) -> tuple[dict[str, dict[str, float]], list[str]]:
    """Build the file→file reference graph from *backend*.

    Returns ``(out_edges, nodes)`` where ``out_edges[src]`` maps each
    destination file to the (float) weight of ref-flow from *src* to it, and
    ``nodes`` is the sorted list of all files appearing as a source or
    destination. Both are deterministic for a given backend (insertion order
    follows the ``ORDER BY`` enumeration).
    """
    out, nodes, _, _ = _collect(backend)
    return out, nodes


def _collect(
    backend: IterableSymbolBackend,
) -> tuple[
    dict[str, dict[str, float]],
    list[str],
    dict[str, list[SymbolDef]],
    Counter[str],
]:
    """Single-pass collection over symbols + refs.

    Returns ``(out_edges, nodes, syms_by_file, ref_count)`` — everything
    :func:`build_graph` and :func:`render_map` need, from **two** enumeration
    passes (one over symbols, one over refs) rather than four. The graph is
    built once and shared by both callers.

    Names defined in more than ``_MAX_DEFS_PER_NAME`` files are treated as
    unresolvable noise and dropped: a bare name like ``init`` / ``get`` defined
    across dozens of files would otherwise explode every reference into dozens
    of low-signal edges (this is what made PageRank infeasible on large repos).
    """
    defs_by_name: dict[str, list[str]] = {}
    syms_by_file: dict[str, list[SymbolDef]] = {}
    for sym in backend.iter_symbols():
        defs_by_name.setdefault(sym.name, []).append(sym.path)
        syms_by_file.setdefault(sym.path, []).append(sym)

    out: dict[str, dict[str, float]] = {}
    dst_nodes: set[str] = set()
    ref_count: Counter[str] = Counter()
    for ref in backend.iter_refs():
        ref_count[ref.name] += 1
        defs = defs_by_name.get(ref.name)
        if not defs or len(defs) > _MAX_DEFS_PER_NAME:
            continue  # external / builtin / too-ambiguous → drop edge
        share = 1.0 / len(defs)
        src_edges: dict[str, float] | None = None
        for dst in defs:
            if dst == ref.path:
                continue  # self-loop → drop
            if src_edges is None:
                src_edges = out.setdefault(ref.path, {})
            src_edges[dst] = src_edges.get(dst, 0.0) + share
            dst_nodes.add(dst)

    nodes = sorted(set(out) | dst_nodes)
    return out, nodes, syms_by_file, ref_count


# --------------------------------------------------------------------------
# PageRank (handwritten power iteration, deterministic)
# --------------------------------------------------------------------------


def pagerank(
    out_edges: dict[str, dict[str, float]],
    nodes: list[str],
    *,
    damping: float = _DAMPING,
    iterations: int = _ITERATIONS,
) -> dict[str, float]:
    """Return ``{file: rank}`` via fixed-count power iteration.

    ``nodes`` must be sorted (deterministic iteration). Dangling nodes (files
    referenced by nothing and referencing nothing get out-weight 0) have their
    rank redistributed evenly, the standard PageRank sink fix.
    """
    n = len(nodes)
    if n == 0:
        return {}
    out_weight = {src: sum(dsts.values()) for src, dsts in out_edges.items()}
    # Dangling nodes (no out-flow) are fixed across iterations — compute once.
    dangling_nodes = [s for s in nodes if out_weight.get(s, 0.0) == 0.0]
    rank = {node: 1.0 / n for node in nodes}
    base = (1.0 - damping) / n
    for _ in range(iterations):
        # Dangling mass redistributed evenly (standard PageRank sink fix).
        dangling_share = damping * sum(rank[s] for s in dangling_nodes) / n
        new: dict[str, float] = {node: base + dangling_share for node in nodes}
        # O(edges): each edge (src→dst) visited exactly once per iteration.
        # (The earlier nodes×edges formulation was infeasible on large repos —
        # 18k nodes × ~500k edges × 30 iters ≈ 2.7e11 ops, i.e. hours.)
        for src, dsts in out_edges.items():
            ow = out_weight[src]
            if ow == 0.0:
                continue  # defensive: src with no real edges is dangling
            share = damping * rank[src] / ow
            for dst, w in dsts.items():
                new[dst] += share * w
        rank = new
    return rank


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def render_map(
    backend: IterableSymbolBackend,
    token_budget: int,
    count_fn: Callable[[str], int],
) -> str:
    """Render a directory-grouped signature tree of the top-ranked files.

    Files are ordered by PageRank desc (path as tie-break); within a file,
    symbols are ordered by reference-count desc (line as tie-break). The tree
    is filled greedily until *token_budget* (counted via *count_fn*) is reached;
    a file that would overflow the remaining budget is skipped and smaller
    later files still get a chance (greedy fill, rank-biased).

    Deterministic for a given backend (see module docstring). Returns ``""``
    when the index has no symbols.
    """
    out_edges, nodes, syms_by_file, ref_count = _collect(backend)
    rank = pagerank(out_edges, nodes)

    if not syms_by_file:
        return ""

    files = sorted(syms_by_file, key=lambda p: (-rank.get(p, 0.0), p))

    lines: list[str] = []
    cur_dir: str | None = None
    used_tokens = 0
    for path in files:
        block = _render_file_block(path, syms_by_file[path], ref_count, cur_dir)
        block_tokens = count_fn(block)
        # +1 accounts for the joining newline between blocks. Strict cap: a
        # file that does not fit is skipped and a smaller later file may still
        # get in (greedy fill, rank-biased). With a realistic 2K budget and
        # <500-token file blocks the map is never empty in practice.
        step = block_tokens + (1 if lines else 0)
        if used_tokens + step > token_budget:
            continue
        lines.append(block)
        used_tokens += step
        cur_dir = str(PurePosixPath(path).parent)
        if cur_dir == ".":
            cur_dir = None
    return "\n".join(lines)


def _render_file_block(
    path: str,
    syms: list[SymbolDef],
    ref_count: Counter[str],
    cur_dir: str | None,
) -> str:
    """Render one file's directory header (on change) + symbol signatures."""
    parent = PurePosixPath(path).parent
    dir_name: str | None = str(parent) if str(parent) != "." else None
    parts: list[str] = []
    if dir_name is not None and dir_name != cur_dir:
        parts.append(f"{dir_name}/")
    indent = "    " if dir_name is not None else "  "
    parts.append(f"  {PurePosixPath(path).name}")
    ordered = sorted(syms, key=lambda s: (-ref_count.get(s.name, 0), s.line, s.name))
    for sym in ordered:
        parts.append(f"{indent}{sym.signature}")
    return "\n".join(parts)


__all__ = ["build_graph", "pagerank", "render_map"]
