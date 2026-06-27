"""Integration test: CLI end-to-end with a mock LLM provider.

Runs the full `krodo` command via Typer's CliRunner (no subprocess fork),
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

from krodo.cli.main import app
from krodo.core.types import LLMChunk, Message, ToolCall, ToolDef

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

    def count_tokens(self, text: str) -> int:
        return 0

    def count_message_tokens(self, messages: list[Message]) -> int:
        return sum(len(m.content) // 4 if isinstance(m.content, str) else 10 for m in messages)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_provider(responses: list[Message]):  # type: ignore[no-untyped-def]
    """Context manager that replaces LiteLLMProvider with the fake."""
    return patch(
        "krodo.cli.main.LiteLLMProvider",
        return_value=_FakeLLMProvider(responses),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_cli_direct_answer(tmp_path: Path) -> None:
    """Krodo replies with a final answer without any tool calls."""
    runner = CliRunner()
    responses = [Message(role="assistant", content="Hello from Krodo!")]

    with _patch_provider(responses):
        result = runner.invoke(
            app,
            ["--root", str(tmp_path), "--approval", "full_auto", "say hello"],
        )

    assert result.exit_code == 0, result.output
    assert "Hello from Krodo!" in result.output


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
    assert "krodo" in result.output.lower()


def test_cli_creates_session_files(tmp_path: Path) -> None:
    """After a run: session JSONL in .krodo/sessions/ and app log in .krodo/logs/."""
    runner = CliRunner()
    responses = [Message(role="assistant", content="logged")]

    with _patch_provider(responses):
        runner.invoke(
            app,
            ["--root", str(tmp_path), "log this"],
        )

    # Session events → .krodo/sessions/*.jsonl
    sessions_dir = tmp_path / ".krodo" / "sessions"
    assert sessions_dir.is_dir()
    session_files = list(sessions_dir.glob("*.jsonl"))
    assert len(session_files) >= 1

    # Application log → .krodo/logs/*.log (NOT .jsonl)
    log_dir = tmp_path / ".krodo" / "logs"
    assert log_dir.is_dir()
    log_files = list(log_dir.glob("*.log"))
    assert len(log_files) >= 1
    # Old mixed .jsonl should no longer exist in logs dir
    old_jsonl_files = list(log_dir.glob("*.jsonl"))
    assert len(old_jsonl_files) == 0


def test_cli_writes_session_init_header(tmp_path: Path) -> None:
    """After a run, the session file's first line is a SESSION_INIT event."""
    import json as _json  # noqa: PLC0415

    runner = CliRunner()
    responses = [Message(role="assistant", content="hi")]

    with _patch_provider(responses):
        runner.invoke(
            app,
            ["--root", str(tmp_path), "say hi"],
        )

    sessions_dir = tmp_path / ".krodo" / "sessions"
    jsonl_files = list(sessions_dir.glob("*.jsonl"))
    assert len(jsonl_files) >= 1

    first_line = jsonl_files[0].read_text().splitlines()[0]
    obj = _json.loads(first_line)
    assert obj["type"] == "session_init"
    assert obj["seq"] == 0
    assert "model" in obj["data"]


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


def test_cli_glob_tool_chain(tmp_path: Path) -> None:
    """LLM calls glob → result → final answer. Tests M2 search tool registration."""
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("y = 2\n")

    runner = CliRunner()
    tc = ToolCall(id="tc-2", name="glob", arguments={"pattern": "**/*.py", "path": "."})
    responses = [
        Message(role="assistant", content="", tool_calls=[tc]),
        Message(role="assistant", content="Found 2 Python files."),
    ]

    with _patch_provider(responses):
        result = runner.invoke(
            app,
            ["--root", str(tmp_path), "--approval", "full_auto", "find all .py files"],
        )

    assert result.exit_code == 0, result.output
    assert "Found 2 Python files." in result.output


def test_cli_edit_file_tool_chain(tmp_path: Path) -> None:
    """LLM calls edit_file → result → final answer. Tests write tool registration."""
    (tmp_path / "code.py").write_text("x = 1\n")

    runner = CliRunner()
    tc = ToolCall(
        id="tc-3",
        name="edit_file",
        arguments={"path": "code.py", "old_string": "x = 1", "new_string": "x = 42"},
    )
    responses = [
        Message(role="assistant", content="", tool_calls=[tc]),
        Message(role="assistant", content="Done: x is now 42."),
    ]

    with _patch_provider(responses):
        result = runner.invoke(
            app,
            ["--root", str(tmp_path), "--approval", "full_auto", "change x to 42"],
        )

    assert result.exit_code == 0, result.output
    assert "Done: x is now 42." in result.output
    assert (tmp_path / "code.py").read_text() == "x = 42\n"


def test_cli_full_auto_warning_visible(tmp_path: Path) -> None:
    """full_auto mode must print a red warning banner."""
    runner = CliRunner()
    responses = [Message(role="assistant", content="done")]

    with _patch_provider(responses):
        result = runner.invoke(
            app,
            ["--root", str(tmp_path), "--approval", "full_auto", "do something"],
        )

    assert result.exit_code == 0, result.output
    assert "full_auto" in result.output or "WARNING" in result.output


def test_cli_read_only_denies_write(tmp_path: Path) -> None:
    """In read_only mode, write_file calls are denied and the agent sees the denial."""
    (tmp_path / "existing.txt").write_text("original")

    runner = CliRunner()
    tc = ToolCall(
        id="tc-4",
        name="write_file",
        arguments={"path": "existing.txt", "content": "overwritten"},
    )
    responses = [
        Message(role="assistant", content="", tool_calls=[tc]),
        Message(role="assistant", content="I cannot write files in read_only mode."),
    ]

    with _patch_provider(responses):
        result = runner.invoke(
            app,
            ["--root", str(tmp_path), "--approval", "read_only", "overwrite file"],
        )

    assert result.exit_code == 0, result.output
    # File must NOT be modified
    assert (tmp_path / "existing.txt").read_text() == "original"


# ---------------------------------------------------------------------------
# M4.8: max_tokens flag wiring (CLI > env > default)
# ---------------------------------------------------------------------------


def _spy_provider(responses: list[Message]):  # type: ignore[no-untyped-def]
    """Like _patch_provider but returns the MagicMock so call_args can be inspected."""
    from unittest.mock import MagicMock  # noqa: PLC0415

    spy = MagicMock(return_value=_FakeLLMProvider(responses))
    return patch("krodo.cli.main.LiteLLMProvider", spy), spy


def test_cli_max_tokens_default_is_16384(tmp_path: Path) -> None:
    """No --max-tokens, no KRODO_MAX_TOKENS → provider gets max_tokens=16384."""
    runner = CliRunner()
    responses = [Message(role="assistant", content="done")]
    patcher, spy = _spy_provider(responses)

    with patcher:
        result = runner.invoke(
            app,
            ["--root", str(tmp_path), "--approval", "full_auto", "go"],
            env={"KRODO_MAX_TOKENS": ""},  # clear env in case host has it set
        )

    assert result.exit_code == 0, result.output
    spy.assert_called_once()
    ck = spy.call_args.kwargs
    assert ck.get("extra_kwargs") == {"max_tokens": 16384}


def test_cli_max_tokens_env_override(tmp_path: Path) -> None:
    """KRODO_MAX_TOKENS=8192 → LiteLLMProvider gets extra_kwargs={'max_tokens': 8192}."""
    runner = CliRunner()
    responses = [Message(role="assistant", content="done")]
    patcher, spy = _spy_provider(responses)

    with patcher:
        result = runner.invoke(
            app,
            ["--root", str(tmp_path), "--approval", "full_auto", "go"],
            env={"KRODO_MAX_TOKENS": "8192"},
        )

    assert result.exit_code == 0, result.output
    ck = spy.call_args.kwargs
    assert ck.get("extra_kwargs") == {"max_tokens": 8192}


def test_cli_max_tokens_cli_overrides_env(tmp_path: Path) -> None:
    """--max-tokens 4096 (with KRODO_MAX_TOKENS=8192) → CLI wins, provider gets 4096."""
    runner = CliRunner()
    responses = [Message(role="assistant", content="done")]
    patcher, spy = _spy_provider(responses)

    with patcher:
        result = runner.invoke(
            app,
            [
                "--root",
                str(tmp_path),
                "--approval",
                "full_auto",
                "--max-tokens",
                "4096",
                "go",
            ],
            env={"KRODO_MAX_TOKENS": "8192"},
        )

    assert result.exit_code == 0, result.output
    ck = spy.call_args.kwargs
    assert ck.get("extra_kwargs") == {"max_tokens": 4096}


def test_cli_banner_shows_max_output(tmp_path: Path) -> None:
    """The compression banner line must include 'Max output:' and the configured value."""
    runner = CliRunner()
    responses = [Message(role="assistant", content="done")]

    with _patch_provider(responses):
        result = runner.invoke(
            app,
            [
                "--root",
                str(tmp_path),
                "--approval",
                "full_auto",
                "--max-tokens",
                "12345",
                "go",
            ],
        )

    assert result.exit_code == 0, result.output
    assert "Max output" in result.output
    assert "12,345" in result.output
