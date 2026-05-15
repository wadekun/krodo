"""Tests for ApplyPatchTool (udiff atomic transaction)."""

from __future__ import annotations

import logging
import textwrap
from pathlib import Path

import pytest

from coda.core.workspace import LocalWorkspaceResolver
from coda.sandbox.firewall import LocalSandboxRunner
from coda.tools.builtin.patch import ApplyPatchTool
from coda.tools.protocols import ToolContext


def _ctx(tmp_path: Path) -> ToolContext:
    ws = LocalWorkspaceResolver().resolve(explicit=tmp_path)
    sb = LocalSandboxRunner(ws)
    return ToolContext(
        workspace=ws,
        sandbox=sb,
        session_id="test",
        logger=logging.getLogger("test"),
    )


def _make_patch(
    source_file: str,
    target_file: str,
    original: str,
    modified: str,
) -> str:
    """Build a minimal two-file unified diff header for tests."""
    import difflib

    original_lines = original.splitlines(keepends=True)
    modified_lines = modified.splitlines(keepends=True)
    diff = list(
        difflib.unified_diff(
            original_lines,
            modified_lines,
            fromfile=f"a/{source_file}",
            tofile=f"b/{target_file}",
        )
    )
    return "".join(diff)


# ---------------------------------------------------------------------------
# Happy paths — single file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_patch_single_file(tmp_path: Path) -> None:
    original = "x = 1\ny = 2\nz = 3\n"
    (tmp_path / "f.py").write_text(original)
    patch = _make_patch("f.py", "f.py", original, "x = 1\ny = 99\nz = 3\n")
    ctx = _ctx(tmp_path)
    result = await ApplyPatchTool().execute({"patch": patch}, ctx)
    assert not result.is_error, result.content
    assert (tmp_path / "f.py").read_text() == "x = 1\ny = 99\nz = 3\n"
    assert "1 file(s)" in result.content


@pytest.mark.asyncio
async def test_apply_patch_new_file(tmp_path: Path) -> None:
    """Patch that creates a brand-new file (source is /dev/null)."""
    patch = textwrap.dedent("""\
        --- /dev/null
        +++ b/new_file.py
        @@ -0,0 +1,3 @@
        +line1
        +line2
        +line3
    """)
    ctx = _ctx(tmp_path)
    result = await ApplyPatchTool().execute({"patch": patch}, ctx)
    assert not result.is_error, result.content
    assert (tmp_path / "new_file.py").exists()
    assert "line1" in (tmp_path / "new_file.py").read_text()


@pytest.mark.asyncio
async def test_apply_patch_lf_file(tmp_path: Path) -> None:
    """Patch to a LF file writes LF output."""
    original = "a\nb\nc\n"
    (tmp_path / "lf.txt").write_text(original, newline="\n")
    patch = _make_patch("lf.txt", "lf.txt", original, "a\nB\nc\n")
    ctx = _ctx(tmp_path)
    result = await ApplyPatchTool().execute({"patch": patch}, ctx)
    assert not result.is_error, result.content
    data = (tmp_path / "lf.txt").read_bytes()
    assert b"\r\n" not in data
    assert b"B\n" in data


@pytest.mark.asyncio
async def test_apply_patch_crlf_file(tmp_path: Path) -> None:
    """Patch to a CRLF file preserves CRLF line endings."""
    original_lf = "a\nb\nc\n"
    original_crlf = original_lf.replace("\n", "\r\n")
    (tmp_path / "crlf.txt").write_bytes(original_crlf.encode())
    # Build patch using LF text (the engine normalises internally)
    patch = _make_patch("crlf.txt", "crlf.txt", original_lf, "a\nB\nc\n")
    ctx = _ctx(tmp_path)
    result = await ApplyPatchTool().execute({"patch": patch}, ctx)
    assert not result.is_error, result.content
    data = (tmp_path / "crlf.txt").read_bytes()
    assert b"\r\nB\r\n" in data


@pytest.mark.asyncio
async def test_apply_patch_no_trailing_newline(tmp_path: Path) -> None:
    """Patch to a file without a trailing newline should still work."""
    original = "hello"  # no trailing \n
    (tmp_path / "noeol.txt").write_text(original)
    modified = "world"
    patch = _make_patch("noeol.txt", "noeol.txt", original + "\n", modified + "\n")
    ctx = _ctx(tmp_path)
    result = await ApplyPatchTool().execute({"patch": patch}, ctx)
    assert not result.is_error, result.content


# ---------------------------------------------------------------------------
# Multi-file atomic transaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_patch_multi_file_success(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("y = 2\n")
    patch_a = _make_patch("a.py", "a.py", "x = 1\n", "x = 10\n")
    patch_b = _make_patch("b.py", "b.py", "y = 2\n", "y = 20\n")
    ctx = _ctx(tmp_path)
    result = await ApplyPatchTool().execute({"patch": patch_a + patch_b}, ctx)
    assert not result.is_error, result.content
    assert (tmp_path / "a.py").read_text() == "x = 10\n"
    assert (tmp_path / "b.py").read_text() == "y = 20\n"
    assert "2 file(s)" in result.content


@pytest.mark.asyncio
async def test_apply_patch_partial_failure_rolls_back(tmp_path: Path) -> None:
    """When the second file's patch fails, both files must be rolled back."""
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("y = 2\n")

    # a.py patch is valid
    patch_a = _make_patch("a.py", "a.py", "x = 1\n", "x = 10\n")
    # b.py patch has wrong context (expects "y = 999")
    patch_b = textwrap.dedent("""\
        --- a/b.py
        +++ b/b.py
        @@ -1,1 +1,1 @@
        -y = 999
        +y = 20
    """)
    ctx = _ctx(tmp_path)
    result = await ApplyPatchTool().execute({"patch": patch_a + patch_b}, ctx)
    assert result.is_error
    # Both files must be rolled back
    assert (tmp_path / "a.py").read_text() == "x = 1\n"
    assert (tmp_path / "b.py").read_text() == "y = 2\n"


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_patch_file_not_exist(tmp_path: Path) -> None:
    patch = _make_patch("ghost.py", "ghost.py", "x = 1\n", "x = 2\n")
    ctx = _ctx(tmp_path)
    result = await ApplyPatchTool().execute({"patch": patch}, ctx)
    assert result.is_error
    assert "does not exist" in result.content


@pytest.mark.asyncio
async def test_apply_patch_invalid_udiff(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = await ApplyPatchTool().execute({"patch": "this is not a valid diff"}, ctx)
    assert result.is_error


@pytest.mark.asyncio
async def test_apply_patch_empty_patch(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = await ApplyPatchTool().execute({"patch": ""}, ctx)
    assert result.is_error


@pytest.mark.asyncio
async def test_apply_patch_path_traversal_denied(tmp_path: Path) -> None:
    patch = textwrap.dedent("""\
        --- a/../../../etc/passwd
        +++ b/../../../etc/passwd
        @@ -1,1 +1,1 @@
        -root:x:0:0:root:/root:/bin/bash
        +evil:x:0:0:evil:/root:/bin/bash
    """)
    ctx = _ctx(tmp_path)
    result = await ApplyPatchTool().execute({"patch": patch}, ctx)
    assert result.is_error
    assert "outside workspace" in result.content


@pytest.mark.asyncio
async def test_apply_patch_removed_file(tmp_path: Path) -> None:
    """Patch that deletes a file (source /dev/null as target)."""
    (tmp_path / "to_delete.txt").write_text("old content\n")
    patch = textwrap.dedent("""\
        --- a/to_delete.txt
        +++ /dev/null
        @@ -1,1 +0,0 @@
        -old content
    """)
    ctx = _ctx(tmp_path)
    result = await ApplyPatchTool().execute({"patch": patch}, ctx)
    assert not result.is_error, result.content
    assert not (tmp_path / "to_delete.txt").exists()


@pytest.mark.asyncio
async def test_apply_patch_hunk_context_mismatch_error(tmp_path: Path) -> None:
    """Patch whose context lines don't match should return an error."""
    (tmp_path / "f.py").write_text("actual line\n")
    patch = textwrap.dedent("""\
        --- a/f.py
        +++ b/f.py
        @@ -1,1 +1,1 @@
        -different line
        +new line
    """)
    ctx = _ctx(tmp_path)
    result = await ApplyPatchTool().execute({"patch": patch}, ctx)
    assert result.is_error
    assert "hunk application failed" in result.content or "mismatch" in result.content
