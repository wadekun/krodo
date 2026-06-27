"""Tests for M6.5 — approval trust persistence across resume.

Covers:
  - export_state / restore_state round-trip on TerminalApprovalManager
  - replay_events applying the last APPROVAL_DECISION state snapshot
  - e2e: 'a' (session trust) answered in session 1 is honoured after resume
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from krodo.cli.main import app
from krodo.core.context import InMemoryContextManager
from krodo.core.types import (
    Message,
    SessionEvent,
    SessionEventType,
    ToolCall,
    ToolDef,
)
from krodo.memory.replay import replay_events
from krodo.sandbox.approval import PatternRule, TerminalApprovalManager

# ---------------------------------------------------------------------------
# export_state / restore_state
# ---------------------------------------------------------------------------


class TestExportRestoreState:
    def test_round_trip(self) -> None:
        mgr = TerminalApprovalManager(mode="auto_edit")
        mgr.trust_session("write_file")
        mgr.trust_session("run_shell")
        mgr.add_pattern_rule(PatternRule(tool_name="run_shell", arg_glob="pytest *"))

        state = mgr.export_state()
        assert state == {
            "trusted_tools": ["run_shell", "write_file"],
            "pattern_rules": [{"tool_name": "run_shell", "arg_glob": "pytest *"}],
        }

        restored = TerminalApprovalManager(mode="auto_edit")
        restored.restore_state(state)
        assert restored.export_state() == state

    @pytest.mark.asyncio
    async def test_restored_trust_approves_without_prompt(self) -> None:
        mgr = TerminalApprovalManager(mode="auto_edit")
        mgr.restore_state(
            {
                "trusted_tools": ["write_file"],
                "pattern_rules": [{"tool_name": "run_shell", "arg_glob": "pytest *"}],
            }
        )

        decision = await mgr.check(
            ToolCall(id="t1", name="write_file", arguments={"path": "a.txt"})
        )
        assert decision == "approve_session"

        decision = await mgr.check(
            ToolCall(id="t2", name="run_shell", arguments={"cmd": "pytest -q"})
        )
        assert decision == "approve_pattern"

    def test_restore_is_additive_and_idempotent(self) -> None:
        mgr = TerminalApprovalManager(mode="auto_edit")
        mgr.trust_session("edit_file")
        state = {
            "trusted_tools": ["write_file"],
            "pattern_rules": [{"tool_name": "run_shell", "arg_glob": "*"}],
        }
        mgr.restore_state(state)
        mgr.restore_state(state)  # second apply must not duplicate

        out = mgr.export_state()
        assert out["trusted_tools"] == ["edit_file", "write_file"]
        assert out["pattern_rules"] == [{"tool_name": "run_shell", "arg_glob": "*"}]

    def test_restore_tolerates_malformed_state(self) -> None:
        mgr = TerminalApprovalManager(mode="auto_edit")
        mgr.restore_state({})
        mgr.restore_state({"trusted_tools": "nope", "pattern_rules": [{"arg_glob": "*"}, 42]})
        assert mgr.export_state() == {"trusted_tools": [], "pattern_rules": []}


# ---------------------------------------------------------------------------
# replay_events applies the snapshot
# ---------------------------------------------------------------------------


def _event(seq: int, etype: SessionEventType, data: dict[str, Any]) -> SessionEvent:
    return SessionEvent(
        id=str(uuid.uuid4()),
        session_id="sess",
        seq=seq,
        type=etype,
        timestamp=datetime.now(UTC),
        data=data,
    )


class TestReplayAppliesApprovalState:
    def test_last_state_snapshot_wins(self) -> None:
        events = [
            _event(0, SessionEventType.USER_MESSAGE, {"content": "hi"}),
            _event(
                1,
                SessionEventType.APPROVAL_DECISION,
                {
                    "tool_name": "write_file",
                    "decision": "approve_session",
                    "state": {"trusted_tools": ["write_file"], "pattern_rules": []},
                },
            ),
            _event(
                2,
                SessionEventType.APPROVAL_DECISION,
                {
                    "tool_name": "run_shell",
                    "decision": "approve_pattern",
                    "state": {
                        "trusted_tools": ["write_file"],
                        "pattern_rules": [{"tool_name": "run_shell", "arg_glob": "git *"}],
                    },
                },
            ),
            _event(3, SessionEventType.ASSISTANT_MESSAGE, {"content": "done"}),
        ]
        ctx = InMemoryContextManager(system_prompt="sys")
        mgr = TerminalApprovalManager(mode="auto_edit")

        replay_events(events, ctx, approval=mgr)

        state = mgr.export_state()
        assert state["trusted_tools"] == ["write_file"]
        assert state["pattern_rules"] == [{"tool_name": "run_shell", "arg_glob": "git *"}]

    def test_no_approval_manager_keeps_old_behaviour(self) -> None:
        events = [
            _event(0, SessionEventType.USER_MESSAGE, {"content": "hi"}),
            _event(
                1,
                SessionEventType.APPROVAL_DECISION,
                {"tool_name": "write_file", "decision": "approve", "state": {"x": 1}},
            ),
        ]
        ctx = InMemoryContextManager(system_prompt="sys")
        stats = replay_events(events, ctx)  # no approval kwarg — must not raise
        assert stats.turns == 1

    def test_decisions_without_state_are_skipped(self) -> None:
        events = [
            _event(
                0,
                SessionEventType.APPROVAL_DECISION,
                {"tool_name": "write_file", "decision": "approve"},
            ),
        ]
        ctx = InMemoryContextManager(system_prompt="sys")
        mgr = TerminalApprovalManager(mode="auto_edit")
        replay_events(events, ctx, approval=mgr)
        assert mgr.export_state() == {"trusted_tools": [], "pattern_rules": []}


# ---------------------------------------------------------------------------
# E2E: 'a' in session 1 survives krodo resume
# ---------------------------------------------------------------------------


class _ScriptedProvider:
    name = "fake"
    model = "fake/model"

    def __init__(self, responses: list[Message]) -> None:
        self._responses = list(responses)
        self._idx = 0

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None = None,
        **kwargs: Any,
    ) -> Message:
        msg = self._responses[self._idx]
        self._idx = min(self._idx + 1, len(self._responses) - 1)
        return msg

    def count_tokens(self, text: str) -> int:
        return 0

    def count_message_tokens(self, messages: list[Message]) -> int:
        return 0


def _script_inputs(seq: list[str]):  # type: ignore[no-untyped-def]
    it = iter(seq)

    def _fake_input(prompt: str = "") -> str:  # noqa: ARG001
        try:
            return next(it)
        except StopIteration as exc:
            raise EOFError from exc

    return _fake_input


def test_session_trust_survives_resume(tmp_path: Path) -> None:
    runner = CliRunner()

    # ---------------- Session 1: user answers 'a' to a write_file prompt ----
    provider1 = _ScriptedProvider(
        [
            Message(
                role="assistant",
                content="writing",
                tool_calls=[
                    ToolCall(
                        id="tc-1",
                        name="write_file",
                        arguments={"path": "first.txt", "content": "one"},
                    )
                ],
            ),
            Message(role="assistant", content="done one"),
        ]
    )
    with (
        patch("krodo.cli.main.LiteLLMProvider", return_value=provider1),
        patch("builtins.input", _script_inputs(["a"])),
    ):
        result1 = runner.invoke(
            app,
            ["--root", str(tmp_path), "--approval", "auto_edit", "write first file"],
        )
    assert result1.exit_code == 0, result1.output
    assert (tmp_path / "first.txt").read_text() == "one"

    session_files = list((tmp_path / ".krodo" / "sessions").glob("*.jsonl"))
    assert len(session_files) == 1
    session_id = session_files[0].stem

    # APPROVAL_DECISION event carries the trust snapshot
    decisions = [
        json.loads(line)
        for line in session_files[0].read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line)["type"] == "approval_decision"
    ]
    assert any(d["data"].get("state", {}).get("trusted_tools") == ["write_file"] for d in decisions)

    # ---------------- Session 2: resume — same tool must NOT prompt ---------
    from krodo.cli.resume import resume_command  # noqa: PLC0415

    provider2 = _ScriptedProvider(
        [
            Message(
                role="assistant",
                content="writing again",
                tool_calls=[
                    ToolCall(
                        id="tc-2",
                        name="write_file",
                        arguments={"path": "second.txt", "content": "two"},
                    )
                ],
            ),
            Message(role="assistant", content="done two"),
        ]
    )
    # Scripted inputs contain NO approval answer: if the prompt appeared it
    # would consume "exit" and the write would never happen.
    with (
        patch("krodo.cli.main.LiteLLMProvider", return_value=provider2),
        patch("builtins.input", _script_inputs(["write second file", "exit"])),
    ):
        resume_command(
            session_id=session_id,
            root=tmp_path,
            approval="auto_edit",
            _workspace_root=tmp_path,
        )

    assert (tmp_path / "second.txt").read_text() == "two"
