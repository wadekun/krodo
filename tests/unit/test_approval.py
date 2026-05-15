"""Tests for TerminalApprovalManager — three-mode matrix + pattern trust."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from coda.core.types import ToolCall
from coda.sandbox.approval import (
    _NO_APPROVAL_TOOLS,
    PatternRule,
    TerminalApprovalManager,
    _match_pattern,
)

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
@pytest.mark.parametrize("tool_name", sorted(_NO_APPROVAL_TOOLS))
async def test_read_only_allows_all_read_tools(tool_name: str) -> None:
    mgr = TerminalApprovalManager(mode="read_only")
    decision = await mgr.check(_call(tool_name))
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


@pytest.mark.asyncio
async def test_read_only_denies_edit_file() -> None:
    mgr = TerminalApprovalManager(mode="read_only")
    decision = await mgr.check(_call("edit_file", path="f.py", old_string="x", new_string="y"))
    assert decision == "deny"


@pytest.mark.asyncio
async def test_read_only_denies_git_commit() -> None:
    mgr = TerminalApprovalManager(mode="read_only")
    decision = await mgr.check(_call("git_commit", message="test"))
    assert decision == "deny"


@pytest.mark.asyncio
async def test_read_only_denies_apply_patch() -> None:
    mgr = TerminalApprovalManager(mode="read_only")
    decision = await mgr.check(_call("apply_patch", patch="--- a\n+++ b\n"))
    assert decision == "deny"


# ---------------------------------------------------------------------------
# full_auto mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_auto_approves_read_tools() -> None:
    mgr = TerminalApprovalManager(mode="full_auto")
    for name in _NO_APPROVAL_TOOLS:
        decision = await mgr.check(_call(name))
        assert decision == "approve", f"Expected approve for {name}"


@pytest.mark.asyncio
async def test_full_auto_approves_write_tools() -> None:
    mgr = TerminalApprovalManager(mode="full_auto")
    for name in ["write_file", "edit_file", "run_shell", "apply_patch", "git_commit"]:
        decision = await mgr.check(_call(name))
        assert decision == "approve", f"Expected approve for {name}"


# ---------------------------------------------------------------------------
# auto_edit mode — no-approval tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", sorted(_NO_APPROVAL_TOOLS))
async def test_auto_edit_no_approval_tools(tool_name: str) -> None:
    mgr = TerminalApprovalManager(mode="auto_edit")
    decision = await mgr.check(_call(tool_name))
    assert decision == "approve"


# ---------------------------------------------------------------------------
# auto_edit — interactive prompt: y / n / a
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_edit_prompt_y_returns_approve() -> None:
    mgr = TerminalApprovalManager(mode="auto_edit")
    with patch("builtins.input", return_value="y"):
        decision = await mgr.check(_call("write_file", path="foo.py", content="x"))
    assert decision == "approve"


@pytest.mark.asyncio
async def test_auto_edit_prompt_n_returns_deny() -> None:
    mgr = TerminalApprovalManager(mode="auto_edit")
    with patch("builtins.input", return_value="n"):
        decision = await mgr.check(_call("run_shell", command="make test"))
    assert decision == "deny"


@pytest.mark.asyncio
async def test_auto_edit_prompt_a_trusts_session() -> None:
    mgr = TerminalApprovalManager(mode="auto_edit")
    with patch("builtins.input", return_value="a"):
        decision = await mgr.check(_call("write_file", path="foo.py", content="x"))
    assert decision == "approve_session"
    # Subsequent call must NOT prompt again
    decision2 = await mgr.check(_call("write_file", path="bar.py", content="y"))
    assert decision2 == "approve_session"


@pytest.mark.asyncio
async def test_auto_edit_prompt_question_then_y() -> None:
    """'?' shows help and re-prompts; 'y' on second attempt approves."""
    mgr = TerminalApprovalManager(mode="auto_edit")
    call_count = 0

    def _input(_: str = "") -> str:  # noqa: ANN001
        nonlocal call_count
        call_count += 1
        return "?" if call_count == 1 else "y"

    with patch("builtins.input", side_effect=_input):
        decision = await mgr.check(_call("write_file", path="foo.py", content="x"))
    assert decision == "approve"
    assert call_count == 2


# ---------------------------------------------------------------------------
# Pattern trust — _match_pattern unit tests
# ---------------------------------------------------------------------------


def test_match_pattern_wildcard_matches_any_call() -> None:
    rules = [PatternRule(tool_name="git_status", arg_glob="*")]
    assert _match_pattern(_call("git_status"), rules)


def test_match_pattern_different_tool_does_not_match() -> None:
    rules = [PatternRule(tool_name="git_status", arg_glob="*")]
    assert not _match_pattern(_call("git_commit", message="x"), rules)


def test_match_pattern_path_glob_matches() -> None:
    rules = [PatternRule(tool_name="read_file", arg_glob="src/coda/*")]
    assert _match_pattern(_call("read_file", path="src/coda/main.py"), rules)


def test_match_pattern_path_glob_no_match() -> None:
    rules = [PatternRule(tool_name="read_file", arg_glob="src/coda/*")]
    assert not _match_pattern(_call("read_file", path="tests/test_main.py"), rules)


def test_match_pattern_shell_cmd_prefix() -> None:
    rules = [PatternRule(tool_name="run_shell", arg_glob="pytest*")]
    assert _match_pattern(_call("run_shell", cmd="pytest tests/ -v"), rules)
    assert not _match_pattern(_call("run_shell", cmd="rm -rf /"), rules)


def test_match_pattern_multiple_rules() -> None:
    rules = [
        PatternRule(tool_name="read_file", arg_glob="docs/*"),
        PatternRule(tool_name="git_status", arg_glob="*"),
    ]
    assert _match_pattern(_call("read_file", path="docs/README.md"), rules)
    assert _match_pattern(_call("git_status"), rules)
    assert not _match_pattern(_call("write_file", path="docs/README.md"), rules)


# ---------------------------------------------------------------------------
# Pattern trust — approve_pattern decision
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_edit_prompt_p_adds_pattern_and_approves() -> None:
    """Typing 'p' then a valid pattern results in approve_pattern."""
    mgr = TerminalApprovalManager(mode="auto_edit")
    inputs = iter(["p", "write_file src/*"])

    with patch("builtins.input", side_effect=inputs):
        decision = await mgr.check(_call("write_file", path="src/foo.py", content="x"))
    assert decision == "approve_pattern"
    # Rule must be stored
    assert len(mgr._pattern_trust) == 1  # noqa: SLF001
    assert mgr._pattern_trust[0].tool_name == "write_file"  # noqa: SLF001


@pytest.mark.asyncio
async def test_pattern_trust_matches_subsequent_calls() -> None:
    """Once a pattern is registered, matching calls are auto-approved."""
    mgr = TerminalApprovalManager(mode="auto_edit")
    mgr.add_pattern_rule(PatternRule(tool_name="run_shell", arg_glob="pytest*"))
    decision = await mgr.check(_call("run_shell", cmd="pytest tests/ -q"))
    assert decision == "approve_pattern"


# ---------------------------------------------------------------------------
# trust_session helper
# ---------------------------------------------------------------------------


def test_trust_session_marks_tool_trusted() -> None:
    mgr = TerminalApprovalManager(mode="auto_edit")
    mgr.trust_session("write_file")
    assert "write_file" in mgr._session_trusted  # noqa: SLF001
