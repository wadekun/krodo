"""AGENTS.md 3-tier merge — load and inject project memory (M5.3).

Architecture §5.6 specifies a three-tier merge of AGENTS.md files:

  Tier 1 — System:  ``~/.config/krodo/AGENTS.md``
  Tier 2 — Project: ``<workspace_root>/AGENTS.md``
  Tier 3 — Subdir:  walk from ``cwd`` up to ``workspace_root``, collecting
                    any AGENTS.md files found in intermediate directories
                    (deepest dir's file appended last).

Truncation rules (§5.6.2):
  - Per-file limit: 8 K tokens (keep first 7 K + last 1 K + marker).
  - Total limit:    12 K tokens.  When exceeded, drop tiers in order:
                    system tier first, then subdir files (oldest/outermost
                    first), never the project tier.

Token counting: delegates to a caller-supplied ``count_fn`` (typically
``provider.count_tokens``); falls back to ``len(text) // 4`` if omitted.

Usage::

    from krodo.memory.agents_md import load_agents_md, AgentsMdBundle

    bundle = load_agents_md(workspace, cwd=Path.cwd(), count_fn=provider.count_tokens)
    if bundle.content:
        ctx._history.insert(0, Message(role="user",
            content=f"<project_memory>\\n{bundle.content}\\n</project_memory>"))
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from krodo.core.workspace import Workspace


_PER_FILE_LIMIT_TOKENS = 8_192
_PER_FILE_HEAD_TOKENS = 7_168  # first 7 K
_PER_FILE_TAIL_TOKENS = 1_024  # last 1 K
_TOTAL_LIMIT_TOKENS = 12_288
_TRUNCATION_MARKER = "\n\n[... truncated ...]\n\n"


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentsMdBundle:
    """Result of the 3-tier AGENTS.md merge.

    Attributes
    ----------
    content:
        The merged (and possibly truncated) text, ready to be injected as a
        ``<project_memory>`` user message.
    sources:
        Paths of the files that contributed to the bundle, in the order they
        were merged.  Useful for the SESSION_INIT ``agents_md_hash`` field and
        for displaying a debug banner.
    total_tokens:
        Estimated total token count of ``content``.
    truncated:
        True if any per-file or total truncation occurred.
    """

    content: str
    sources: list[Path] = field(default_factory=list)
    total_tokens: int = 0
    truncated: bool = False

    def sha256(self) -> str | None:
        """Return the SHA-256 hash of *content*, or None if content is empty."""
        if not self.content:
            return None
        return hashlib.sha256(self.content.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------


def load_agents_md(
    workspace: Workspace,
    *,
    cwd: Path | None = None,
    count_fn: Callable[[str], int] | None = None,
) -> AgentsMdBundle:
    """Merge AGENTS.md files from all three tiers into a single bundle.

    Parameters
    ----------
    workspace:
        Active Workspace; provides ``root`` (project root).
    cwd:
        Current working directory; used for the subdir walk (tier 3).
        Defaults to ``workspace.root`` if not supplied.
    count_fn:
        Token counting function ``(text: str) -> int``.  Falls back to
        ``len(text) // 4`` if omitted.
    """
    count = count_fn or _default_count

    effective_cwd = (cwd or workspace.root).resolve()

    # ------------------------------------------------------------------
    # Collect raw texts per tier
    # ------------------------------------------------------------------

    # Tier 1: system
    system_path = Path.home() / ".config" / "krodo" / "AGENTS.md"
    tier1: list[tuple[Path, str]] = []
    if system_path.exists():
        tier1 = [(system_path, _read(system_path))]

    # Tier 2: project
    project_path = workspace.root / "AGENTS.md"
    tier2: list[tuple[Path, str]] = []
    if project_path.exists():
        tier2 = [(project_path, _read(project_path))]

    # Tier 3: subdirectory walk (cwd → project_root, intermediate only)
    tier3 = workspace.discover_subdir_agents_md(effective_cwd)

    # ------------------------------------------------------------------
    # Per-file truncation
    # ------------------------------------------------------------------

    truncated = False
    sources: list[Path] = []
    parts: list[str] = []  # in merge order: tier1 → tier2 → tier3

    for path, text in [*tier1, *tier2, *tier3]:
        tok = count(text)
        if tok > _PER_FILE_LIMIT_TOKENS:
            text = _truncate_file(text, count)
            truncated = True
        sources.append(path)
        parts.append(text)

    # ------------------------------------------------------------------
    # Total truncation
    # ------------------------------------------------------------------

    # Recompute token totals after per-file truncation
    token_counts = [count(p) for p in parts]
    total = sum(token_counts)

    if total > _TOTAL_LIMIT_TOKENS:
        truncated = True
        # Drop in order: tier1 first, then tier3 files (outermost first),
        # never tier2 (project tier).
        parts, sources, token_counts, total = _total_truncate(
            parts,
            sources,
            token_counts,
            total,
            tier1_count=len(tier1),
            tier2_count=len(tier2),
        )

    merged = "\n\n---\n\n".join(parts) if parts else ""
    return AgentsMdBundle(
        content=merged,
        sources=sources,
        total_tokens=count(merged) if merged else 0,
        truncated=truncated,
    )


# ---------------------------------------------------------------------------
# Workspace extension (added here; applied via monkeypatch-style method)
# ---------------------------------------------------------------------------


def discover_subdir_agents_md(
    workspace: Workspace,
    cwd: Path,
) -> list[tuple[Path, str]]:
    """Walk *cwd* up to *workspace.root*, collecting intermediate AGENTS.md files.

    Returns the files in outer-to-inner order (outermost dir first,
    deepest last) so that the most-specific rules override earlier ones
    when the bundle is read top-to-bottom.

    Files at *workspace.root* itself (tier 2) and outside it are excluded
    — the caller handles those separately.
    """
    results: list[tuple[Path, str]] = []
    root = workspace.root.resolve()

    try:
        rel = cwd.relative_to(root)
    except ValueError:
        return []

    # Walk from root's direct children down to cwd
    parts_list = rel.parts
    current = root
    for part in parts_list:
        current = current / part
        candidate = current / "AGENTS.md"
        if candidate.exists() and candidate != root / "AGENTS.md":
            results.append((candidate, _read(candidate)))

    return results


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _default_count(text: str) -> int:
    return max(1, len(text) // 4)


def _truncate_file(text: str, count: Callable[[str], int]) -> str:
    """Keep first 7 K tokens + last 1 K tokens with a truncation marker."""
    chars_per_token = max(1, len(text) // max(1, count(text)))
    head_chars = _PER_FILE_HEAD_TOKENS * chars_per_token
    tail_chars = _PER_FILE_TAIL_TOKENS * chars_per_token

    head = text[:head_chars]
    tail = text[-tail_chars:] if tail_chars < len(text) - head_chars else ""
    if tail:
        return head + _TRUNCATION_MARKER + tail
    return head + _TRUNCATION_MARKER


def _total_truncate(
    parts: list[str],
    sources: list[Path],
    token_counts: list[int],
    total: int,
    *,
    tier1_count: int,
    tier2_count: int,
) -> tuple[list[str], list[Path], list[int], int]:
    """Drop parts to bring total under _TOTAL_LIMIT_TOKENS.

    Drop order: tier1 files, then tier3 files (outermost first).
    The tier2 (project) files are never dropped.
    """
    tier2_end = tier1_count + tier2_count

    # Indices eligible for dropping: tier1 then tier3 (after tier2)
    # Tier1: indices 0..tier1_count-1
    # Tier3: indices tier2_end..end
    droppable = list(range(tier1_count)) + list(range(tier2_end, len(parts)))

    drop_set: set[int] = set()
    for idx in droppable:
        if total <= _TOTAL_LIMIT_TOKENS:
            break
        total -= token_counts[idx]
        drop_set.add(idx)

    new_parts = [p for i, p in enumerate(parts) if i not in drop_set]
    new_sources = [s for i, s in enumerate(sources) if i not in drop_set]
    new_counts = [c for i, c in enumerate(token_counts) if i not in drop_set]
    return new_parts, new_sources, new_counts, total
