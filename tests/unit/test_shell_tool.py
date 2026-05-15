"""Tests for RunShellTool."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from coda.core.workspace import LocalWorkspaceResolver
from coda.sandbox.firewall import LocalSandboxRunner
from coda.tools.builtin.shell import RunShellTool
from coda.tools.protocols import ToolContext


def _ctx(tmp_path: Path) -> ToolContext:
    ws = LocalWorkspaceResolver().resolve(explicit=tmp_path)
    sb = LocalSandboxRunner(ws)
    return ToolContext(
        workspace=ws, sandbox=sb, session_id="test", logger=logging.getLogger("test")
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_echo_command_succeeds(tmp_path: Path) -> None:
    result = await RunShellTool().execute({"command": "echo hello"}, _ctx(tmp_path))
    assert not result.is_error
    assert "hello" in result.content


@pytest.mark.asyncio
async def test_exit_nonzero_marks_error(tmp_path: Path) -> None:
    result = await RunShellTool().execute({"command": "false"}, _ctx(tmp_path))
    assert result.is_error
    assert "exit" in result.content


# ---------------------------------------------------------------------------
# Blocklist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sudo_blocked(tmp_path: Path) -> None:
    result = await RunShellTool().execute({"command": "sudo ls"}, _ctx(tmp_path))
    assert result.is_error
    assert "DENIED" in result.content


@pytest.mark.asyncio
async def test_rm_rf_root_blocked(tmp_path: Path) -> None:
    result = await RunShellTool().execute(
        {"command": "bash -c 'rm -rf /'"},
        _ctx(tmp_path),
    )
    assert result.is_error
    assert "DENIED" in result.content


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_returns_error(tmp_path: Path) -> None:
    result = await RunShellTool().execute(
        {"command": "sleep 60", "timeout": 0.1},
        _ctx(tmp_path),
    )
    assert result.is_error
    assert "timed out" in result.content or "exit" in result.content


# ---------------------------------------------------------------------------
# cwd
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cwd_defaults_to_workspace_root(tmp_path: Path) -> None:
    result = await RunShellTool().execute({"command": "pwd"}, _ctx(tmp_path))
    assert not result.is_error
    assert str(tmp_path.resolve()) in result.content


@pytest.mark.asyncio
async def test_cwd_subdir(tmp_path: Path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    result = await RunShellTool().execute({"command": "pwd", "cwd": "sub"}, _ctx(tmp_path))
    assert not result.is_error
    assert "sub" in result.content


@pytest.mark.asyncio
async def test_cwd_outside_workspace_denied(tmp_path: Path) -> None:
    result = await RunShellTool().execute(
        {"command": "ls", "cwd": "../../"},
        _ctx(tmp_path),
    )
    assert result.is_error
    assert "outside workspace" in result.content


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_command_error(tmp_path: Path) -> None:
    result = await RunShellTool().execute({"command": ""}, _ctx(tmp_path))
    assert result.is_error


@pytest.mark.asyncio
async def test_python_script_execution(tmp_path: Path) -> None:
    (tmp_path / "hello.py").write_text("print('Hello Coda')\n")
    result = await RunShellTool().execute(
        {"command": f"{sys.executable} hello.py"},
        _ctx(tmp_path),
    )
    assert not result.is_error
    assert "Hello Coda" in result.content
