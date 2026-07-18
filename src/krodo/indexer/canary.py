"""Subprocess canary probe: survive native tree-sitter/py-tree-sitter crashes.

``TreeSitterSymbolIndex.build_full()`` runs tree-sitter parsing in the same
process as the rest of the agent loop. A native crash in the parser/binding
layer (SIGSEGV/SIGBUS — see
``docs/benchmarks/m9_symbol_index_perf_results.md`` for the py-tree-sitter
0.26.0 ``Point`` refcount use-after-free that motivated this module) bypasses
Python's exception machinery entirely and kills the whole session, not just
the index build.

:func:`probe` runs a cheap canary *before* ``build_full()``: it samples the
first ``sample_size`` real, supported source files encountered while walking
the workspace — broad sampling of real files in walk order, not the largest
N by size. The bug that motivated this module only manifests on files with
enough distinct symbols to accumulate heap corruption (see the perf-results
doc's trigger-condition table), so walking real workspace files is a better
predictor than picking a few large ones or using synthetic fixtures — and
parses them in an isolated subprocess. A non-zero exit / signal death means
the native layer is broken on this machine; the caller should disable the
index for the session rather than let the same crash happen in-process during
the real build.

**Residual risk**: this is a best-effort safety net, not a guarantee. The
probe only samples ``sample_size`` files, so a bad file/runtime combination
outside the sample can still crash ``build_full()`` later. The dependency pin
in ``pyproject.toml`` (``tree-sitter<0.26``) is what actually eliminates the
*known* crash; the canary only guards against *future* native regressions
(e.g. a new tree-sitter or language-pack release reintroducing a similar
bug) so a bad update degrades to "no index" instead of killing the session.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from krodo.indexer.extract import supported_extensions

_DEFAULT_SAMPLE_SIZE = 16
_DEFAULT_TIMEOUT_S = 15.0


def sample_files(
    workspace_root: Path,
    ignore: object,
    sample_size: int = _DEFAULT_SAMPLE_SIZE,
) -> list[Path]:
    """Return up to *sample_size* supported source files, in walk order.

    Stops as soon as enough files are collected — no full-tree stat pass, so
    this stays cheap even on a large workspace (thousands of files).

    ``ignore`` is typed loosely (``object``) to avoid a hard import dependency
    on ``krodo.sandbox`` from this module; the concrete type is ``KrodoIgnore``,
    duck-typed here via ``is_ignored(Path) -> bool`` (same convention as
    ``symbol_index.py``).
    """
    exts = supported_extensions()
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(workspace_root):
        rel_dir = Path(dirpath).relative_to(workspace_root)
        dirnames[:] = [d for d in dirnames if not ignore.is_ignored(rel_dir / d)]  # type: ignore[attr-defined]
        for fn in filenames:
            rel = rel_dir / fn
            if rel.suffix.lower() not in exts or ignore.is_ignored(rel):  # type: ignore[attr-defined]
                continue
            out.append(workspace_root / rel)
            if len(out) >= sample_size:
                return out
    return out


def probe(
    workspace_root: Path,
    ignore: object,
    *,
    sample_size: int = _DEFAULT_SAMPLE_SIZE,
    timeout: float = _DEFAULT_TIMEOUT_S,
) -> tuple[bool, str | None]:
    """Parse a sample of real workspace files in a subprocess.

    Returns ``(ok, detail)``: ``ok`` is False when the subprocess died from a
    signal, exited non-zero, or timed out (native crash); *detail* is a short
    human-readable reason for logging, or ``None`` on success. An empty
    sample (e.g. an empty or all-ignored workspace) is treated as success —
    there is nothing to probe.
    """
    files = sample_files(workspace_root, ignore, sample_size)
    if not files:
        return True, None
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell, targets our own module
            [sys.executable, "-m", "krodo.indexer.canary", *[str(f) for f in files]],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"canary probe timed out after {timeout:.0f}s ({len(files)} files sampled)"
    if proc.returncode != 0:
        return False, (
            f"canary probe exited {proc.returncode} ({len(files)} files sampled) "
            "— likely a native parser crash"
        )
    return True, None


def _main(argv: list[str]) -> int:
    """Subprocess entry point: parse every path in *argv*.

    A native crash (SIGSEGV/SIGBUS) kills this subprocess with a non-zero or
    negative return code, which :func:`probe` observes from the parent
    process without the parent itself being affected.
    """
    from krodo.indexer.extract import extract_symbols  # noqa: PLC0415

    for raw_path in argv:
        path = Path(raw_path)
        try:
            source = path.read_bytes()
        except OSError:
            continue
        extract_symbols(path.name, source)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
