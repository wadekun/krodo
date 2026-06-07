"""Integration tests for the M4.9 interactive REPL.

Drives `coda` (no prompt argument) through Typer's CliRunner with a scripted
`input()` so we can exercise the multi-turn loop without a real TTY.

The tests focus on REPL-specific contracts (history persistence, exit
conditions, summary timing) and complement the existing headless-mode tests
in `test_cli_e2e.py`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

from typer.testing import CliRunner

from coda.cli.main import app
from coda.core.types import LLMChunk, Message, ToolDef


class _ScriptedProvider:
    """LLM provider that returns successive scripted messages and records calls.

    Each REPL turn invokes `chat()` exactly once (no tool calls), so the
    `calls` list records every assistant message that was produced in
    response to user input.  Tests inspect `messages_seen` to verify
    that prior-turn history is being sent on the next turn.
    """

    def __init__(self, responses: list[Message]) -> None:
        self._responses = list(responses)
        self._idx = 0
        self.calls: int = 0
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


def _script_inputs(seq: list[str]):  # type: ignore[no-untyped-def]
    """Return a function that yields the next scripted input on each call.

    Used to monkeypatch `builtins.input` so the REPL can be driven from
    tests without a real TTY.
    """
    it = iter(seq)

    def _fake_input(prompt: str = "") -> str:  # noqa: ARG001
        try:
            return next(it)
        except StopIteration as exc:
            # Defensive: if the REPL keeps asking after the script is
            # exhausted we send EOF rather than hanging.
            raise EOFError from exc

    return _fake_input


def _make_provider_patcher(provider: _ScriptedProvider):  # type: ignore[no-untyped-def]
    return patch("coda.cli.main.LiteLLMProvider", return_value=provider)


def test_repl_multi_turn_preserves_history(tmp_path: Path) -> None:
    """Second turn's messages must include the first turn's exchange."""
    provider = _ScriptedProvider(
        [
            Message(role="assistant", content="answer one"),
            Message(role="assistant", content="answer two"),
        ]
    )
    runner = CliRunner()

    with (
        _make_provider_patcher(provider),
        patch("builtins.input", _script_inputs(["first question", "second question", "exit"])),
    ):
        result = runner.invoke(app, ["--root", str(tmp_path), "--approval", "full_auto"])

    assert result.exit_code == 0, result.output
    assert provider.calls == 2, f"expected 2 LLM calls, got {provider.calls}"

    # First turn: only the user message (plus system prompt) should be in history.
    first_user_msgs = [m for m in provider.messages_seen[0] if m.role == "user"]
    assert any("first question" in (m.content or "") for m in first_user_msgs)

    # Second turn: history must now include both user messages and the first
    # assistant reply — that is the whole point of REPL multi-turn.
    second_users = [m for m in provider.messages_seen[1] if m.role == "user"]
    second_assistants = [m for m in provider.messages_seen[1] if m.role == "assistant"]
    assert any("first question" in (m.content or "") for m in second_users)
    assert any("second question" in (m.content or "") for m in second_users)
    assert any("answer one" in (m.content or "") for m in second_assistants)

    # Both replies should be echoed to the user.
    assert "answer one" in result.output
    assert "answer two" in result.output


def test_repl_exits_on_exit_command(tmp_path: Path) -> None:
    """Typing 'exit' as the first input must quit without calling the LLM."""
    provider = _ScriptedProvider([Message(role="assistant", content="never used")])
    runner = CliRunner()

    with (
        _make_provider_patcher(provider),
        patch("builtins.input", _script_inputs(["exit"])),
    ):
        result = runner.invoke(app, ["--root", str(tmp_path), "--approval", "full_auto"])

    assert result.exit_code == 0, result.output
    assert provider.calls == 0
    assert "session summary" in result.output  # stderr is captured into output here


def test_repl_exits_on_eof(tmp_path: Path) -> None:
    """Ctrl-D (EOFError from input) exits cleanly and prints summary."""
    provider = _ScriptedProvider([Message(role="assistant", content="never used")])
    runner = CliRunner()

    def _raise_eof(prompt: str = "") -> str:  # noqa: ARG001
        raise EOFError

    with (
        _make_provider_patcher(provider),
        patch("builtins.input", _raise_eof),
    ):
        result = runner.invoke(app, ["--root", str(tmp_path), "--approval", "full_auto"])

    assert result.exit_code == 0, result.output
    assert provider.calls == 0
    assert "session summary" in result.output


def test_repl_skips_empty_input(tmp_path: Path) -> None:
    """Empty and whitespace-only inputs are ignored; only real prompts hit the LLM."""
    provider = _ScriptedProvider([Message(role="assistant", content="single answer")])
    runner = CliRunner()

    with (
        _make_provider_patcher(provider),
        patch("builtins.input", _script_inputs(["", "   ", "real task", "exit"])),
    ):
        result = runner.invoke(app, ["--root", str(tmp_path), "--approval", "full_auto"])

    assert result.exit_code == 0, result.output
    assert provider.calls == 1, (
        f"empty inputs should be skipped, expected 1 LLM call, got {provider.calls}"
    )
    assert "single answer" in result.output


def test_repl_session_summary_printed_once_on_exit(tmp_path: Path) -> None:
    """Two turns + exit must produce exactly one '─── session summary' block."""
    provider = _ScriptedProvider(
        [
            Message(role="assistant", content="reply A"),
            Message(role="assistant", content="reply B"),
        ]
    )
    runner = CliRunner()

    with (
        _make_provider_patcher(provider),
        patch("builtins.input", _script_inputs(["task one", "task two", "exit"])),
    ):
        result = runner.invoke(app, ["--root", str(tmp_path), "--approval", "full_auto"])

    assert result.exit_code == 0, result.output
    assert provider.calls == 2
    assert result.output.count("session summary") == 1
    # REPL summary line must show the cumulative turn count.
    assert "turns" in result.output
    assert "2" in result.output  # the count itself


def test_repl_no_prompt_does_not_print_help(tmp_path: Path) -> None:
    """Regression check for the removed `ctx.get_help()` branch."""
    provider = _ScriptedProvider([Message(role="assistant", content="hi")])
    runner = CliRunner()

    with (
        _make_provider_patcher(provider),
        patch("builtins.input", _script_inputs(["exit"])),
    ):
        result = runner.invoke(app, ["--root", str(tmp_path), "--approval", "full_auto"])

    assert result.exit_code == 0, result.output
    # The old behaviour printed `Usage: coda [OPTIONS] [PROMPT]`.  That line
    # must NOT appear when entering REPL mode.
    assert "Usage:" not in result.output
    assert "REPL mode" in result.output


# ---------------------------------------------------------------------------
# TTY path — prompt_toolkit branch
# ---------------------------------------------------------------------------


def test_repl_tty_path_uses_prompt_toolkit(tmp_path: Path) -> None:
    """On a real TTY the REPL uses PromptSession.prompt_async.

    We patch sys.stdin.isatty to return True and replace prompt_async with
    an AsyncMock that yields scripted lines then raises EOFError, verifying
    that:
      - prompt_toolkit's prompt_async is called (not builtins.input),
      - multi-turn history is still preserved across turns,
      - the REPL exits cleanly on EOFError from prompt_async.
    """
    from prompt_toolkit import PromptSession  # noqa: PLC0415

    provider = _ScriptedProvider(
        [
            Message(role="assistant", content="ptk answer one"),
            Message(role="assistant", content="ptk answer two"),
        ]
    )

    scripted = ["ptk question one", "ptk question two"]
    call_count = 0

    async def _fake_prompt_async(self: object, prompt: str = "") -> str:  # noqa: ARG001
        nonlocal call_count
        if call_count < len(scripted):
            result = scripted[call_count]
            call_count += 1
            return result
        raise EOFError

    runner = CliRunner()
    with (
        _make_provider_patcher(provider),
        # Make the REPL believe stdin is a TTY so it takes the prompt_toolkit branch.
        patch("coda.cli.repl.sys") as mock_sys,
        patch.object(PromptSession, "prompt_async", new=_fake_prompt_async),
    ):
        mock_sys.stdin.isatty.return_value = True
        result = runner.invoke(app, ["--root", str(tmp_path), "--approval", "full_auto"])

    assert result.exit_code == 0, result.output
    assert provider.calls == 2, f"expected 2 LLM calls, got {provider.calls}"

    # Multi-turn history: second turn must have seen both user messages.
    second_users = [m for m in provider.messages_seen[1] if m.role == "user"]
    assert any("ptk question one" in (m.content or "") for m in second_users)
    assert any("ptk question two" in (m.content or "") for m in second_users)

    assert "ptk answer one" in result.output
    assert "ptk answer two" in result.output
