"""Integration tests for the M6.4 REPL slash commands.

Drives `coda` (no prompt) through CliRunner with scripted `input()`, same
pattern as test_cli_repl.py.  Verifies that slash commands are handled
locally (the LLM never sees them) and that `:resume <id>` switches sessions
with history intact.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

from typer.testing import CliRunner

from coda.cli.main import app
from coda.core.types import Message, ToolDef


class _ScriptedProvider:
    """Returns scripted messages; records every message list it sees."""

    name = "fake"
    model = "fake/model"

    def __init__(self, responses: list[Message]) -> None:
        self._responses = list(responses)
        self._idx = 0
        self.calls = 0
        self.messages_seen: list[list[Message]] = []

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None = None,
        **kwargs: Any,
    ) -> Message:
        self.calls += 1
        self.messages_seen.append(list(messages))
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


def _patch_provider(provider: _ScriptedProvider):  # type: ignore[no-untyped-def]
    return patch("coda.cli.main.LiteLLMProvider", return_value=provider)


def test_help_lists_commands_without_llm_call(tmp_path: Path) -> None:
    provider = _ScriptedProvider([Message(role="assistant", content="never")])
    runner = CliRunner()

    with (
        _patch_provider(provider),
        patch("builtins.input", _script_inputs([":help", "exit"])),
    ):
        result = runner.invoke(app, ["--root", str(tmp_path), "--approval", "full_auto"])

    assert result.exit_code == 0, result.output
    assert provider.calls == 0
    assert ":sessions" in result.output
    assert ":resume" in result.output


def test_sessions_renders_table(tmp_path: Path) -> None:
    provider = _ScriptedProvider([Message(role="assistant", content="never")])
    runner = CliRunner()

    with (
        _patch_provider(provider),
        patch("builtins.input", _script_inputs([":sessions", "exit"])),
    ):
        result = runner.invoke(app, ["--root", str(tmp_path), "--approval", "full_auto"])

    assert result.exit_code == 0, result.output
    assert provider.calls == 0
    # The current session was just created, so the table has at least one row.
    assert "Recent sessions" in result.output


def test_unknown_slash_command_hints_help_and_skips_llm(tmp_path: Path) -> None:
    provider = _ScriptedProvider([Message(role="assistant", content="never")])
    runner = CliRunner()

    with (
        _patch_provider(provider),
        patch("builtins.input", _script_inputs([":wat", "exit"])),
    ):
        result = runner.invoke(app, ["--root", str(tmp_path), "--approval", "full_auto"])

    assert result.exit_code == 0, result.output
    assert provider.calls == 0
    assert ":help" in result.output


def test_cost_command_reports_totals(tmp_path: Path) -> None:
    provider = _ScriptedProvider(
        [
            Message(
                role="assistant",
                content="costly answer",
                usage={"prompt_tokens": 1000, "completion_tokens": 200},
                cost_usd=0.0123,
            )
        ]
    )
    runner = CliRunner()

    with (
        _patch_provider(provider),
        patch("builtins.input", _script_inputs(["do something", ":cost", "exit"])),
    ):
        result = runner.invoke(app, ["--root", str(tmp_path), "--approval", "full_auto"])

    assert result.exit_code == 0, result.output
    assert provider.calls == 1
    assert "tokens: 1.0k in / 200 out" in result.output
    assert "cost $0.0123" in result.output


def test_cost_command_without_usage(tmp_path: Path) -> None:
    provider = _ScriptedProvider([Message(role="assistant", content="never")])
    runner = CliRunner()

    with (
        _patch_provider(provider),
        patch("builtins.input", _script_inputs([":cost", "exit"])),
    ):
        result = runner.invoke(app, ["--root", str(tmp_path), "--approval", "full_auto"])

    assert result.exit_code == 0, result.output
    assert "No token usage recorded" in result.output


def test_undo_error_does_not_kill_repl(tmp_path: Path) -> None:
    """:undo in a fresh (checkpoint-less) session prints an error; REPL survives."""
    provider = _ScriptedProvider([Message(role="assistant", content="still alive")])
    runner = CliRunner()

    with (
        _patch_provider(provider),
        patch("builtins.input", _script_inputs([":undo", "follow-up task", "exit"])),
    ):
        result = runner.invoke(app, ["--root", str(tmp_path), "--approval", "full_auto"])

    assert result.exit_code == 0, result.output
    assert "No checkpoint found" in result.output
    # The REPL kept running and the next prompt reached the LLM.
    assert provider.calls == 1
    assert "still alive" in result.output


def test_resume_unknown_id_stays_in_repl(tmp_path: Path) -> None:
    provider = _ScriptedProvider([Message(role="assistant", content="never")])
    runner = CliRunner()

    with (
        _patch_provider(provider),
        patch("builtins.input", _script_inputs([":resume zzzzzzzz", "exit"])),
    ):
        result = runner.invoke(app, ["--root", str(tmp_path), "--approval", "full_auto"])

    assert result.exit_code == 0, result.output
    assert provider.calls == 0
    assert "No session matching 'zzzzzzzz'" in result.output


def test_resume_switches_session_with_history(tmp_path: Path) -> None:
    """`:resume <old-id>` rebuilds components; the next turn sees old history."""
    runner = CliRunner()

    # Session 1: headless run that leaves a session file behind.
    provider1 = _ScriptedProvider([Message(role="assistant", content="original answer")])
    with _patch_provider(provider1):
        result1 = runner.invoke(
            app,
            ["--root", str(tmp_path), "--approval", "full_auto", "original question"],
        )
    assert result1.exit_code == 0, result1.output

    session_files = list((tmp_path / ".coda" / "sessions").glob("*.jsonl"))
    assert len(session_files) == 1
    old_id = session_files[0].stem

    # Session 2: REPL — switch to session 1, then ask a follow-up.
    provider2 = _ScriptedProvider([Message(role="assistant", content="follow-up answer")])
    with (
        _patch_provider(provider2),
        patch(
            "builtins.input",
            _script_inputs([f":resume {old_id}", "follow-up question", "exit"]),
        ),
    ):
        result2 = runner.invoke(app, ["--root", str(tmp_path), "--approval", "full_auto"])

    assert result2.exit_code == 0, result2.output
    assert f"Switching to session {old_id}" in result2.output
    assert provider2.calls == 1

    # The follow-up turn must include the replayed history from session 1.
    seen = provider2.messages_seen[0]
    users = [m for m in seen if m.role == "user"]
    assistants = [m for m in seen if m.role == "assistant"]
    assert any("original question" in str(m.content) for m in users)
    assert any("original answer" in str(m.content) for m in assistants)
    assert any("follow-up question" in str(m.content) for m in users)
    assert "follow-up answer" in result2.output
