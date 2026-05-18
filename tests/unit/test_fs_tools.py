"""Tests for ReadFileTool and WriteFileTool."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from coda.core.workspace import LocalWorkspaceResolver
from coda.sandbox.firewall import LocalSandboxRunner
from coda.sandbox.ignore import CodaIgnore
from coda.tools.builtin.fs import ReadFileTool, WriteFileTool
from coda.tools.protocols import ToolContext


def _ctx(tmp_path: Path) -> ToolContext:
    ws = LocalWorkspaceResolver().resolve(explicit=tmp_path)
    sb = LocalSandboxRunner(ws)
    return ToolContext(
        workspace=ws,
        sandbox=sb,
        session_id="test",
        logger=logging.getLogger("test"),
        ignore=CodaIgnore(tmp_path),
    )


# ---------------------------------------------------------------------------
# ReadFileTool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_file_happy_path(tmp_path: Path) -> None:
    (tmp_path / "hello.py").write_text("print('hi')\n")
    ctx = _ctx(tmp_path)
    result = await ReadFileTool().execute({"path": "hello.py"}, ctx)
    assert not result.is_error
    assert "print('hi')" in result.content


@pytest.mark.asyncio
async def test_read_file_missing(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = await ReadFileTool().execute({"path": "nope.py"}, ctx)
    assert result.is_error
    assert "does not exist" in result.content


@pytest.mark.asyncio
async def test_read_file_path_traversal(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = await ReadFileTool().execute({"path": "../../../etc/passwd"}, ctx)
    assert result.is_error
    assert "outside workspace" in result.content


@pytest.mark.asyncio
async def test_read_file_absolute_path_denied(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = await ReadFileTool().execute({"path": "/etc/passwd"}, ctx)
    assert result.is_error
    assert "outside workspace" in result.content


@pytest.mark.asyncio
async def test_read_file_symlink_escape_denied(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside_read"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    link = tmp_path / "link.txt"
    link.symlink_to(outside / "secret.txt")
    ctx = _ctx(tmp_path)
    result = await ReadFileTool().execute({"path": "link.txt"}, ctx)
    assert result.is_error
    assert "outside workspace" in result.content


@pytest.mark.asyncio
async def test_read_file_with_offset_limit(tmp_path: Path) -> None:
    lines = [f"line {i}\n" for i in range(1, 11)]
    (tmp_path / "file.txt").write_text("".join(lines))
    ctx = _ctx(tmp_path)
    result = await ReadFileTool().execute({"path": "file.txt", "offset": 3, "limit": 2}, ctx)
    assert not result.is_error
    assert "line 3" in result.content
    assert "line 4" in result.content
    assert "line 5" not in result.content


@pytest.mark.asyncio
async def test_read_file_not_a_file(tmp_path: Path) -> None:
    (tmp_path / "subdir").mkdir()
    ctx = _ctx(tmp_path)
    result = await ReadFileTool().execute({"path": "subdir"}, ctx)
    assert result.is_error
    assert "not a regular file" in result.content


@pytest.mark.asyncio
async def test_read_file_ignored_by_codaignore(tmp_path: Path) -> None:
    """read_file on a .env file returns PathIgnoredError (hardcoded default)."""
    (tmp_path / ".env").write_text("SECRET=abc\n")
    ctx = _ctx(tmp_path)
    result = await ReadFileTool().execute({"path": ".env"}, ctx)
    assert result.is_error
    assert "PathIgnoredError" in result.content
    assert ".env" in result.content
    assert "hardcoded" in result.content


# ---------------------------------------------------------------------------
# WriteFileTool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_file_creates_new(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = await WriteFileTool().execute({"path": "out.py", "content": "x = 1\n"}, ctx)
    assert not result.is_error
    assert (tmp_path / "out.py").read_text() == "x = 1\n"


@pytest.mark.asyncio
async def test_write_file_creates_directories(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = await WriteFileTool().execute(
        {"path": "deep/nested/file.txt", "content": "hello"}, ctx
    )
    assert not result.is_error
    assert (tmp_path / "deep" / "nested" / "file.txt").exists()


@pytest.mark.asyncio
async def test_write_file_traversal_denied(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = await WriteFileTool().execute(
        {"path": "../../../tmp/evil.sh", "content": "rm -rf /"}, ctx
    )
    assert result.is_error
    assert "outside workspace" in result.content


@pytest.mark.asyncio
async def test_write_file_absolute_path_denied(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = await WriteFileTool().execute({"path": "/tmp/evil.sh", "content": "evil"}, ctx)
    assert result.is_error


@pytest.mark.asyncio
async def test_write_file_overwrites_existing(tmp_path: Path) -> None:
    (tmp_path / "existing.txt").write_text("old content")
    ctx = _ctx(tmp_path)
    result = await WriteFileTool().execute({"path": "existing.txt", "content": "new content"}, ctx)
    assert not result.is_error
    assert (tmp_path / "existing.txt").read_text() == "new content"


# ---------------------------------------------------------------------------
# M3: SHA-256 conflict detection in EditFileTool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_file_detects_external_modification(tmp_path: Path) -> None:
    """edit_file must return an error if the file was externally modified since read."""
    from coda.tools.builtin.fs import EditFileTool, ReadFileTool, _sha256_cache

    ctx = _ctx(tmp_path)
    target = tmp_path / "target.py"
    target.write_text("original content\n")

    # First: read the file so SHA256 is cached
    await ReadFileTool().execute({"path": "target.py"}, ctx)
    cached = _sha256_cache.get(str(target))
    assert cached is not None

    # Externally modify the file (simulating another process)
    target.write_text("externally modified content\n")

    # Now try to edit — should detect the conflict
    result = await EditFileTool().execute(
        {"path": "target.py", "old_string": "original content", "new_string": "new content"},
        ctx,
    )
    assert result.is_error
    assert "externally" in result.content.lower() or "modified" in result.content.lower()


@pytest.mark.asyncio
async def test_edit_file_succeeds_when_no_external_change(tmp_path: Path) -> None:
    """edit_file must succeed when the file hasn't been externally modified."""
    from coda.tools.builtin.fs import EditFileTool, ReadFileTool

    ctx = _ctx(tmp_path)
    target = tmp_path / "target.py"
    target.write_text("old content\n")

    # Read then edit — no external modification
    await ReadFileTool().execute({"path": "target.py"}, ctx)
    result = await EditFileTool().execute(
        {"path": "target.py", "old_string": "old content", "new_string": "new content"},
        ctx,
    )
    assert not result.is_error


@pytest.mark.asyncio
async def test_edit_file_no_cache_allows_edit(tmp_path: Path) -> None:
    """edit_file must succeed when no SHA256 is cached (first-time edit without prior read)."""
    from coda.tools.builtin.fs import EditFileTool, _sha256_cache

    ctx = _ctx(tmp_path)
    target = tmp_path / "fresh.py"
    target.write_text("original text\n")

    # Ensure no cache entry exists
    _sha256_cache.pop(str(target), None)

    result = await EditFileTool().execute(
        {"path": "fresh.py", "old_string": "original text", "new_string": "updated text"},
        ctx,
    )
    assert not result.is_error
