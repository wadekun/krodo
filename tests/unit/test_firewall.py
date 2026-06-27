"""Tests for LocalSandboxRunner — path firewall, blocklist, subprocess execution."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from krodo.core.workspace import LocalWorkspaceResolver
from krodo.sandbox.firewall import BLOCKLIST_FIRST_TOKEN, LocalSandboxRunner


def _make_sandbox(tmp_path: Path) -> LocalSandboxRunner:
    ws = LocalWorkspaceResolver().resolve(explicit=tmp_path)
    return LocalSandboxRunner(ws)


# ---------------------------------------------------------------------------
# is_path_allowed — path firewall
# ---------------------------------------------------------------------------


def test_path_inside_root_is_allowed(tmp_path: Path) -> None:
    sb = _make_sandbox(tmp_path)
    assert sb.is_path_allowed(tmp_path / "src" / "main.py")


def test_path_at_root_is_allowed(tmp_path: Path) -> None:
    sb = _make_sandbox(tmp_path)
    assert sb.is_path_allowed(tmp_path)


def test_parent_of_root_is_denied(tmp_path: Path) -> None:
    sb = _make_sandbox(tmp_path)
    assert not sb.is_path_allowed(tmp_path.parent)


def test_sibling_directory_is_denied(tmp_path: Path) -> None:
    sibling = tmp_path.parent / "sibling"
    sb = _make_sandbox(tmp_path)
    assert not sb.is_path_allowed(sibling)


def test_absolute_etc_passwd_denied(tmp_path: Path) -> None:
    sb = _make_sandbox(tmp_path)
    assert not sb.is_path_allowed(Path("/etc/passwd"))


def test_symlink_escaping_root_is_denied(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    link = tmp_path / "link"
    link.symlink_to(outside / "secret.txt")
    sb = _make_sandbox(tmp_path)
    # The resolved path of the symlink is outside the workspace
    assert not sb.is_path_allowed(link)


# ---------------------------------------------------------------------------
# is_command_allowed — blocklist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cmd", [["sudo", "apt", "install", "vim"], ["su", "-"]])
def test_blocklist_first_token(tmp_path: Path, cmd: list[str]) -> None:
    sb = _make_sandbox(tmp_path)
    assert not sb.is_command_allowed(cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        ["bash", "-c", "rm -rf /"],
        ["sh", "-c", "mkfs.ext4 /dev/sda"],
        ["echo", ":(){:|:&};:"],
        ["bash", "-c", "dd if=/dev/zero of=/dev/sda"],
    ],
)
def test_blocklist_substring(tmp_path: Path, cmd: list[str]) -> None:
    sb = _make_sandbox(tmp_path)
    assert not sb.is_command_allowed(cmd)


def test_safe_command_allowed(tmp_path: Path) -> None:
    sb = _make_sandbox(tmp_path)
    assert sb.is_command_allowed(["python", "-m", "pytest"])


def test_empty_command_denied(tmp_path: Path) -> None:
    sb = _make_sandbox(tmp_path)
    assert not sb.is_command_allowed([])


# ---------------------------------------------------------------------------
# run — asyncio subprocess
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_simple_echo(tmp_path: Path) -> None:
    sb = _make_sandbox(tmp_path)
    code, out, err = await sb.run(["echo", "hello"], cwd=tmp_path, timeout=10)
    assert code == 0
    assert "hello" in out
    assert err == ""


@pytest.mark.asyncio
async def test_run_nonzero_exit(tmp_path: Path) -> None:
    sb = _make_sandbox(tmp_path)
    code, out, err = await sb.run(["false"], cwd=tmp_path, timeout=10)
    assert code != 0


@pytest.mark.asyncio
async def test_run_missing_command(tmp_path: Path) -> None:
    sb = _make_sandbox(tmp_path)
    code, out, err = await sb.run(
        ["this_command_definitely_does_not_exist_xyz"], cwd=tmp_path, timeout=10
    )
    assert code != 0
    assert "not found" in err or "No such" in err


@pytest.mark.asyncio
async def test_run_timeout(tmp_path: Path) -> None:
    sb = _make_sandbox(tmp_path)
    code, out, err = await sb.run(["sleep", "60"], cwd=tmp_path, timeout=0.1)
    assert code == -1
    assert "timed out" in err


@pytest.mark.asyncio
async def test_run_output_truncation(tmp_path: Path) -> None:
    sb = LocalSandboxRunner(
        LocalWorkspaceResolver().resolve(explicit=tmp_path),
        output_limit_bytes=10,
    )
    # Generate output longer than 10 bytes
    cmd = [sys.executable, "-c", "print('A' * 200)"]
    code, out, err = await sb.run(cmd, cwd=tmp_path, timeout=10)
    assert "truncated" in out


# ---------------------------------------------------------------------------
# BLOCKLIST_FIRST_TOKEN sanity
# ---------------------------------------------------------------------------


def test_blocklist_contains_sudo() -> None:
    assert "sudo" in BLOCKLIST_FIRST_TOKEN


def test_blocklist_contains_su() -> None:
    assert "su" in BLOCKLIST_FIRST_TOKEN
