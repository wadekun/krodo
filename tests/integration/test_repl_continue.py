"""Integration tests for REPL continuation after the tool-call limit.

When a turn stops on `hit_tool_call_limit`, the REPL asks
"Tool call limit reached — continue? [y/n]".  `y` re-enters the same turn
with a fresh budget via `AgentLoop.continue_turn()` (no new user message);
`n` returns to the normal prompt.  Also locks in the protocol fix: the
unexecuted tool call from the interrupted batch gets a synthesized
"[skipped]" tool result so the follow-up LLM call has a legal history.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

from typer.testing import CliRunner

from krodo.cli.main import app
from krodo.core.types import Message, ToolCall, ToolDef


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
    return patch("krodo.cli.main.LiteLLMProvider", return_value=provider)


def _two_call_batch() -> Message:
    return Message(
        role="assistant",
        content="",
        tool_calls=[
            ToolCall(id="tc-1", name="read_file", arguments={"path": "a.txt"}),
            ToolCall(id="tc-2", name="read_file", arguments={"path": "b.txt"}),
        ],
    )


def test_continue_after_limit_finishes_task(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("alpha")
    (tmp_path / "b.txt").write_text("beta")

    provider = _ScriptedProvider(
        [
            _two_call_batch(),
            Message(role="assistant", content="finished everything"),
        ]
    )
    runner = CliRunner()

    with (
        _patch_provider(provider),
        patch("builtins.input", _script_inputs(["read both files", "y", "exit"])),
    ):
        result = runner.invoke(
            app,
            ["--root", str(tmp_path), "--approval", "full_auto", "--max-tool-calls", "1"],
        )

    assert result.exit_code == 0, result.output
    assert provider.calls == 2
    assert "finished everything" in result.output

    # The continuation call saw a legal history: the unexecuted tc-2 was
    # paired with a synthesized "[skipped]" result, and no extra user
    # message was injected by continue_turn().
    continuation = provider.messages_seen[1]
    tool_msgs = {m.tool_call_id: m.content for m in continuation if m.role == "tool"}
    assert "tc-1" in tool_msgs
    assert "skipped" in tool_msgs["tc-2"]
    users = [m for m in continuation if m.role == "user"]
    assert len(users) == 1  # only the original prompt


def test_decline_continue_returns_to_prompt(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("alpha")
    (tmp_path / "b.txt").write_text("beta")

    provider = _ScriptedProvider([_two_call_batch()])
    runner = CliRunner()

    with (
        _patch_provider(provider),
        patch("builtins.input", _script_inputs(["read both files", "n", "exit"])),
    ):
        result = runner.invoke(
            app,
            ["--root", str(tmp_path), "--approval", "full_auto", "--max-tool-calls", "1"],
        )

    assert result.exit_code == 0, result.output
    # `n` means no continuation call went to the LLM; REPL exited cleanly
    # (the scripted "exit" was consumed by the next prompt, not the y/n ask).
    assert provider.calls == 1
