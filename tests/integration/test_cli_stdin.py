"""Integration tests for the M6.3 pipe-stdin entry.

`echo task | krodo` must run headless with the piped text as the prompt;
`git diff | krodo "review"` must append the piped text as a <stdin> context
block; empty piped stdin keeps the REPL behaviour unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

from typer.testing import CliRunner

from krodo.cli.main import app
from krodo.core.types import Message, ToolDef


class _RecordingProvider:
    """Returns one canned answer and records every message list it sees."""

    name = "fake"
    model = "fake/model"

    def __init__(self) -> None:
        self.messages_seen: list[list[Message]] = []

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None = None,
        **kwargs: Any,
    ) -> Message:
        self.messages_seen.append(list(messages))
        return Message(role="assistant", content="piped answer")

    def count_tokens(self, text: str) -> int:
        return 0

    def count_message_tokens(self, messages: list[Message]) -> int:
        return 0


def test_piped_stdin_without_prompt_runs_headless(tmp_path: Path) -> None:
    """`echo "do the task" | krodo` → headless run with the piped prompt."""
    provider = _RecordingProvider()
    runner = CliRunner()

    with patch("krodo.cli.main.LiteLLMProvider", return_value=provider):
        result = runner.invoke(
            app,
            ["--root", str(tmp_path), "--approval", "full_auto"],
            input="do the piped task\n",
        )

    assert result.exit_code == 0, result.output
    assert len(provider.messages_seen) == 1
    user_msgs = [m for m in provider.messages_seen[0] if m.role == "user"]
    assert any("do the piped task" in (m.content or "") for m in user_msgs)
    # Headless, not REPL
    assert "REPL mode" not in result.output
    assert "piped answer" in result.output


def test_piped_stdin_with_prompt_becomes_context(tmp_path: Path) -> None:
    """`git diff | krodo "review this"` → prompt + <stdin> block."""
    provider = _RecordingProvider()
    runner = CliRunner()

    with patch("krodo.cli.main.LiteLLMProvider", return_value=provider):
        result = runner.invoke(
            app,
            ["--root", str(tmp_path), "--approval", "full_auto", "review this"],
            input="diff --git a/x b/x\n+added line\n",
        )

    assert result.exit_code == 0, result.output
    user_msgs = [m for m in provider.messages_seen[0] if m.role == "user"]
    combined = "\n".join(str(m.content) for m in user_msgs)
    assert "review this" in combined
    assert "<stdin>" in combined
    assert "diff --git a/x b/x" in combined
    assert "</stdin>" in combined


def test_empty_piped_stdin_enters_repl(tmp_path: Path) -> None:
    """Empty stdin (CliRunner default) without a prompt still enters the REPL."""
    provider = _RecordingProvider()
    runner = CliRunner()

    def _raise_eof(prompt: str = "") -> str:  # noqa: ARG001
        raise EOFError

    with (
        patch("krodo.cli.main.LiteLLMProvider", return_value=provider),
        patch("builtins.input", _raise_eof),
    ):
        result = runner.invoke(app, ["--root", str(tmp_path), "--approval", "full_auto"])

    assert result.exit_code == 0, result.output
    assert "REPL mode" in result.output
    assert len(provider.messages_seen) == 0
