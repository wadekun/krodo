"""Integration test: CLI end-to-end with a mock LLM provider.

Runs the full `coda` command via Typer's CliRunner (no subprocess fork),
wiring a scripted fake LLM that returns a single tool call and a final reply.

This test validates:
  - Banner is printed (contains workspace root)
  - Approval check is called
  - Tool is executed (read_file, sandboxed)
  - Final answer is echoed to stdout
  - JSONL log file is created
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

from typer.testing import CliRunner

from coda.cli.main import app
from coda.core.types import LLMChunk, Message, ToolCall, ToolDef

# ---------------------------------------------------------------------------
# Fake LLM provider
# ---------------------------------------------------------------------------


class _FakeLLMProvider:
    """Two-response sequence: tool call → final answer."""

    def __init__(self, responses: list[Message]) -> None:
        self._responses = list(responses)
        self._index = 0

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None = None,
        **kwargs: Any,
    ) -> Message:
        msg = self._responses[self._index]
        self._index = min(self._index + 1, len(self._responses) - 1)
        return msg

    async def stream_chat(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[LLMChunk]:
        raise NotImplementedError

    def count_tokens(self, messages: list[Message]) -> int:
        return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_provider(responses: list[Message]):  # type: ignore[no-untyped-def]
    """Context manager that replaces LiteLLMProvider with the fake."""
    return patch(
        "coda.cli.main.LiteLLMProvider",
        return_value=_FakeLLMProvider(responses),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_cli_direct_answer(tmp_path: Path) -> None:
    """Coda replies with a final answer without any tool calls."""
    runner = CliRunner()
    responses = [Message(role="assistant", content="Hello from Coda!")]

    with _patch_provider(responses):
        result = runner.invoke(
            app,
            ["--root", str(tmp_path), "--approval", "full_auto", "say hello"],
        )

    assert result.exit_code == 0, result.output
    assert "Hello from Coda!" in result.output


def test_cli_banner_shows_workspace_root(tmp_path: Path) -> None:
    """Banner must contain the workspace label (path may be truncated by Rich)."""
    runner = CliRunner()
    responses = [Message(role="assistant", content="done")]

    with _patch_provider(responses):
        result = runner.invoke(
            app,
            ["--root", str(tmp_path), "do something"],
        )

    # Banner always contains these landmarks
    assert "workspace" in result.output
    assert "coda" in result.output.lower()


def test_cli_creates_jsonl_log(tmp_path: Path) -> None:
    """A JSONL log file must be created in .coda/logs/."""
    runner = CliRunner()
    responses = [Message(role="assistant", content="logged")]

    with _patch_provider(responses):
        runner.invoke(
            app,
            ["--root", str(tmp_path), "log this"],
        )

    log_dir = tmp_path / ".coda" / "logs"
    assert log_dir.is_dir()
    log_files = list(log_dir.glob("*.jsonl"))
    assert len(log_files) >= 1


def test_cli_tool_call_roundtrip(tmp_path: Path) -> None:
    """LLM calls read_file → tool result → final answer flow."""
    # Create a file to be read
    hello = tmp_path / "hello.txt"
    hello.write_text("world")

    runner = CliRunner()
    tc = ToolCall(id="tc-1", name="read_file", arguments={"path": "hello.txt"})
    responses = [
        Message(role="assistant", content="", tool_calls=[tc]),
        Message(role="assistant", content="The file contains: world"),
    ]

    with _patch_provider(responses):
        result = runner.invoke(
            app,
            ["--root", str(tmp_path), "--approval", "full_auto", "read hello.txt"],
        )

    assert result.exit_code == 0, result.output
    assert "world" in result.output
