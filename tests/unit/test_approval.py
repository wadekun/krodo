"""Tests for TerminalApprovalManager."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from coda.core.types import ToolCall
from coda.sandbox.approval import _NO_APPROVAL_TOOLS, TerminalApprovalManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _call(name: str, **kwargs: object) -> ToolCall:
    return ToolCall(id="tc1", name=name, arguments=dict(kwargs))


# ---------------------------------------------------------------------------
# read_only mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_only_allows_read_file() -> None:
    mgr = TerminalApprovalManager(mode="read_only")
    decision = await mgr.check(_call("read_file", path="foo.py"))
    assert decision == "approve"


@pytest.mark.asyncio
async def test_read_only_denies_write_file() -> None:
    mgr = TerminalApprovalManager(mode="read_only")
    decision = await mgr.check(_call("write_file", path="foo.py", content="x"))
    assert decision == "deny"


@pytest.mark.asyncio
async def test_read_only_denies_run_shell() -> None:
    mgr = TerminalApprovalManager(mode="read_only")
    decision = await mgr.check(_call("run_shell", command="ls"))
    assert decision == "deny"


# ---------------------------------------------------------------------------
# full_auto mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_auto_approves_everything() -> None:
    mgr = TerminalApprovalManager(mode="full_auto")
    for name in ["read_file", "write_file", "run_shell"]:
        decision = await mgr.check(_call(name))
        assert decision == "approve"


# ---------------------------------------------------------------------------
# auto_edit mode (default) — no-approval tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", sorted(_NO_APPROVAL_TOOLS))
async def test_auto_edit_no_approval_tools(tool_name: str) -> None:
    mgr = TerminalApprovalManager(mode="auto_edit")
    decision = await mgr.check(_call(tool_name))
    assert decision == "approve"


# ---------------------------------------------------------------------------
# auto_edit — interactive prompt: y
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_edit_prompt_y_returns_approve() -> None:
    mgr = TerminalApprovalManager(mode="auto_edit")
    with patch("builtins.input", return_value="y"):
        decision = await mgr.check(_call("write_file", path="foo.py", content="x"))
    assert decision == "approve"


# ---------------------------------------------------------------------------
# auto_edit — interactive prompt: n
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_edit_prompt_n_returns_deny() -> None:
    mgr = TerminalApprovalManager(mode="auto_edit")
    with patch("builtins.input", return_value="n"):
        decision = await mgr.check(_call("run_shell", command="make test"))
    assert decision == "deny"


# ---------------------------------------------------------------------------
# auto_edit — interactive prompt: a (session trust)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_edit_prompt_a_trusts_session() -> None:
    mgr = TerminalApprovalManager(mode="auto_edit")
    with patch("builtins.input", return_value="a"):
        decision = await mgr.check(_call("write_file", path="foo.py", content="x"))
    assert decision == "approve_session"
    # Subsequent call must NOT prompt again
    decision2 = await mgr.check(_call("write_file", path="bar.py", content="y"))
    assert decision2 == "approve_session"


# ---------------------------------------------------------------------------
# trust_session
# ---------------------------------------------------------------------------


def test_trust_session_marks_tool_trusted() -> None:
    mgr = TerminalApprovalManager(mode="auto_edit")
    mgr.trust_session("write_file")
    assert "write_file" in mgr._session_trusted  # noqa: SLF001
