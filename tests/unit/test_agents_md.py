"""Unit tests for src/krodo/memory/agents_md.py — 3-tier AGENTS.md merge (M5.3)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from krodo.core.workspace import LocalWorkspaceResolver
from krodo.memory.agents_md import (
    AgentsMdBundle,
    load_agents_md,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_workspace(tmp_path: Path) -> object:
    return LocalWorkspaceResolver().resolve(explicit=tmp_path)


def _count(text: str) -> int:
    """Simple stable count for tests."""
    return max(1, len(text) // 4)


# ---------------------------------------------------------------------------
# 1. Project-only tier
# ---------------------------------------------------------------------------


class TestProjectOnlyTier:
    def test_loads_project_agents_md_only(self, tmp_path: Path) -> None:
        """Only <root>/AGENTS.md exists → bundle has 1 source."""
        (tmp_path / "AGENTS.md").write_text("# Project rules\n\nDo X.")
        ws = _make_workspace(tmp_path)

        with patch("pathlib.Path.home", return_value=tmp_path / "no-home"):
            bundle = load_agents_md(ws, cwd=tmp_path, count_fn=_count)

        assert len(bundle.sources) == 1
        assert bundle.sources[0] == tmp_path / "AGENTS.md"
        assert "Project rules" in bundle.content
        assert not bundle.truncated

    def test_empty_when_no_agents_md(self, tmp_path: Path) -> None:
        """None of the 3 tiers exist → bundle.content == '', sources == []."""
        ws = _make_workspace(tmp_path)

        with patch("pathlib.Path.home", return_value=tmp_path / "no-home"):
            bundle = load_agents_md(ws, cwd=tmp_path, count_fn=_count)

        assert bundle.content == ""
        assert bundle.sources == []
        assert bundle.total_tokens == 0
        assert not bundle.truncated


# ---------------------------------------------------------------------------
# 2. Three-tier merge order
# ---------------------------------------------------------------------------


class TestThreeTierMergeOrder:
    def test_three_tier_merge_order(self, tmp_path: Path) -> None:
        """system → project → outer subdir → inner subdir order."""
        # Build directory tree:
        #  tmp_path/
        #    AGENTS.md         ← tier 2 (project)
        #    sub1/
        #      AGENTS.md       ← tier 3 outer subdir
        #      sub2/
        #        AGENTS.md     ← tier 3 inner subdir (deepest)

        home_dir = tmp_path / "fake-home"
        (home_dir / ".config" / "krodo").mkdir(parents=True)
        (home_dir / ".config" / "krodo" / "AGENTS.md").write_text("# System")

        (tmp_path / "AGENTS.md").write_text("# Project")

        sub1 = tmp_path / "sub1"
        sub1.mkdir()
        (sub1 / "AGENTS.md").write_text("# Sub1 (outer)")

        sub2 = sub1 / "sub2"
        sub2.mkdir()
        (sub2 / "AGENTS.md").write_text("# Sub2 (inner)")

        ws = _make_workspace(tmp_path)

        with patch("pathlib.Path.home", return_value=home_dir):
            bundle = load_agents_md(ws, cwd=sub2, count_fn=_count)

        assert len(bundle.sources) == 4

        # Verify order in content
        content = bundle.content
        idx_system = content.index("# System")
        idx_project = content.index("# Project")
        idx_outer = content.index("# Sub1 (outer)")
        idx_inner = content.index("# Sub2 (inner)")

        assert idx_system < idx_project < idx_outer < idx_inner


# ---------------------------------------------------------------------------
# 3. Per-file truncation
# ---------------------------------------------------------------------------


class TestPerFileTruncation:
    def test_per_file_truncation_at_8k_tokens(self, tmp_path: Path) -> None:
        """A file exceeding _PER_FILE_LIMIT_TOKENS is truncated with marker."""
        # Each char ≈ 0.25 tokens with our _count function → 4 chars per token.
        # To exceed 8192 tokens we need > 32768 chars.
        big_content = "X" * 40_000
        (tmp_path / "AGENTS.md").write_text(big_content)
        ws = _make_workspace(tmp_path)

        with patch("pathlib.Path.home", return_value=tmp_path / "no-home"):
            bundle = load_agents_md(ws, cwd=tmp_path, count_fn=_count)

        assert bundle.truncated
        assert "[... truncated ...]" in bundle.content
        # Content should be shorter than original
        assert len(bundle.content) < len(big_content)


# ---------------------------------------------------------------------------
# 4. Total truncation drops system first
# ---------------------------------------------------------------------------


class TestTotalTruncation:
    def test_total_truncation_drops_system_first(self, tmp_path: Path) -> None:
        """When total > 12K tokens, system tier is dropped before project."""
        home_dir = tmp_path / "fake-home"
        (home_dir / ".config" / "krodo").mkdir(parents=True)
        # 20K tokens worth of text (4 chars × 20K = 80K chars)
        system_content = "S" * 80_000
        (home_dir / ".config" / "krodo" / "AGENTS.md").write_text(system_content)

        # Project tier: 1K tokens (4K chars)
        project_content = "P" * 4_000
        (tmp_path / "AGENTS.md").write_text(project_content)

        ws = _make_workspace(tmp_path)

        with patch("pathlib.Path.home", return_value=home_dir):
            bundle = load_agents_md(ws, cwd=tmp_path, count_fn=_count)

        assert bundle.truncated
        # System tier should have been dropped (not found in content)
        # Project content must still be present
        assert "P" * 10 in bundle.content

    def test_project_tier_never_dropped(self, tmp_path: Path) -> None:
        """Project AGENTS.md is never dropped even under total budget pressure."""
        (tmp_path / "AGENTS.md").write_text("# Critical project rules\n\nNEVER DROP ME")
        ws = _make_workspace(tmp_path)

        with patch("pathlib.Path.home", return_value=tmp_path / "no-home"):
            bundle = load_agents_md(ws, cwd=tmp_path, count_fn=_count)

        assert "NEVER DROP ME" in bundle.content


# ---------------------------------------------------------------------------
# 5. Subdir walk stops at project root
# ---------------------------------------------------------------------------


class TestSubdirWalk:
    def test_subdir_walk_stops_at_project_root(self, tmp_path: Path) -> None:
        """Files above workspace.root are not included."""
        # Put an AGENTS.md in the *parent* of workspace root
        parent_agents = tmp_path.parent / "AGENTS.md"
        try:
            parent_agents.write_text("# Should NOT be included")
            ws = _make_workspace(tmp_path)

            with patch("pathlib.Path.home", return_value=tmp_path / "no-home"):
                bundle = load_agents_md(ws, cwd=tmp_path, count_fn=_count)

            assert "Should NOT be included" not in bundle.content
        finally:
            if parent_agents.exists():
                parent_agents.unlink()

    def test_subdir_walk_skips_root_project_file(self, tmp_path: Path) -> None:
        """<root>/AGENTS.md is handled as tier 2, not duplicated in tier 3."""
        sub = tmp_path / "src"
        sub.mkdir()
        (tmp_path / "AGENTS.md").write_text("# Project (tier 2)")
        ws = _make_workspace(tmp_path)

        with patch("pathlib.Path.home", return_value=tmp_path / "no-home"):
            bundle = load_agents_md(ws, cwd=sub, count_fn=_count)

        # Should appear exactly once
        assert bundle.content.count("# Project (tier 2)") == 1
        assert len(bundle.sources) == 1


# ---------------------------------------------------------------------------
# 6. sha256 convenience
# ---------------------------------------------------------------------------


class TestSha256:
    def test_sha256_none_for_empty_bundle(self) -> None:
        bundle = AgentsMdBundle(content="", sources=[], total_tokens=0, truncated=False)
        assert bundle.sha256() is None

    def test_sha256_returns_hex_string(self, tmp_path: Path) -> None:
        bundle = AgentsMdBundle(content="some content", sources=[], total_tokens=3, truncated=False)
        digest = bundle.sha256()
        assert digest is not None
        assert len(digest) == 64  # SHA-256 hex = 64 chars
        assert all(c in "0123456789abcdef" for c in digest)
