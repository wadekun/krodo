"""Integration tests for coda resume subcommand (M5.2)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from coda.cli.main import app
from coda.core.types import LLMChunk, Message, ToolCall, ToolDef
from coda.memory.store import JsonlSessionStore


# ---------------------------------------------------------------------------
# Helpers (mirrors test_cli_e2e.py)
# ---------------------------------------------------------------------------


class _FakeLLMProvider:
    """Scripted fake: returns responses in order, then repeats last one."""

    def __init__(self, responses: list[Message]) -> None:
        self._responses = list(responses)
        self._index = 0
        self.messages_seen: list[list[Message]] = []

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None = None,
        **kwargs: Any,
    ) -> Message:
        self.messages_seen.append(list(messages))
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
        return 0


def _patch_provider(provider: _FakeLLMProvider):  # type: ignore[no-untyped-def]
    return patch("coda.cli.main.LiteLLMProvider", return_value=provider)


# ---------------------------------------------------------------------------
# 1. resume continues conversation
# ---------------------------------------------------------------------------


def test_resume_continues_conversation(tmp_path: Path) -> None:
    """Session 1 headless → resume → REPL turn sees prior history in messages."""
    runner = CliRunner()

    # Session 1: headless run → creates session file
    provider1 = _FakeLLMProvider([Message(role="assistant", content="Hello from session 1")])
    with _patch_provider(provider1):
        result = runner.invoke(
            app,
            ["--root", str(tmp_path), "--approval", "full_auto", "Turn 1 prompt"],
        )
    assert result.exit_code == 0, result.output

    # Find the session that was created
    sessions_dir = tmp_path / ".coda" / "sessions"
    session_files = list(sessions_dir.glob("*.jsonl"))
    assert len(session_files) == 1
    session_id = session_files[0].stem

    # Session 2: resume the session
    # Mock input to provide one user turn then exit
    from coda.cli.resume import resume_command  # noqa: PLC0415

    provider2 = _FakeLLMProvider([Message(role="assistant", content="Hello from session 2")])

    call_args_capture: list[list[Message]] = []

    async def _fake_chat(messages: list[Message], **kwargs: Any) -> Message:
        call_args_capture.append(list(messages))
        return Message(role="assistant", content="Resume reply")

    with patch("coda.cli.main.LiteLLMProvider") as mock_prov:
        instance = mock_prov.return_value
        instance.chat = _fake_chat
        instance.stream_chat = provider2.stream_chat
        instance.count_tokens = provider2.count_tokens
        instance.count_message_tokens = provider2.count_message_tokens

        # Mock input to provide one turn then EOF
        inputs = iter(["Turn 2 prompt", "exit"])
        with patch("builtins.input", side_effect=inputs):
            resume_command(
                session_id=session_id,
                root=tmp_path,
                approval="full_auto",
                _workspace_root=tmp_path,
            )

    # The provider saw at least one call; the messages should contain the prior turn
    assert len(call_args_capture) >= 1
    all_content = " ".join(
        m.content for msg_list in call_args_capture for m in msg_list if isinstance(m.content, str)
    )
    # The resumed session injected the prior user message into history
    assert "Turn 1 prompt" in all_content


# ---------------------------------------------------------------------------
# 2. resume with explicit ID
# ---------------------------------------------------------------------------


def test_resume_with_explicit_id(tmp_path: Path) -> None:
    """Two sessions exist; resume the older one by explicit ID."""
    from coda.memory.store import JsonlSessionStore  # noqa: PLC0415

    store = JsonlSessionStore(tmp_path / ".coda" / "sessions")
    store.create_session("session-aaa", model="test", agents_md_hash=None, initial_prompt_hash=None)
    store.create_session("session-bbb", model="test", agents_md_hash=None, initial_prompt_hash=None)

    from coda.cli.resume import _resolve_session_id  # noqa: PLC0415

    resolved = _resolve_session_id(store, "session-aaa")
    assert resolved == "session-aaa"

    resolved_b = _resolve_session_id(store, "session-bbb")
    assert resolved_b == "session-bbb"


def test_resolve_session_id_prefix_match(tmp_path: Path) -> None:
    """Prefix match resolves to full ID when unambiguous."""
    from coda.memory.store import JsonlSessionStore  # noqa: PLC0415

    store = JsonlSessionStore(tmp_path / ".coda" / "sessions")
    store.create_session("abcdef12", model="test", agents_md_hash=None, initial_prompt_hash=None)

    from coda.cli.resume import _resolve_session_id  # noqa: PLC0415

    resolved = _resolve_session_id(store, "abcd")
    assert resolved == "abcdef12"


def test_resolve_session_id_none_returns_most_recent(tmp_path: Path) -> None:
    """No session_id → returns most recent."""
    from coda.memory.store import JsonlSessionStore  # noqa: PLC0415

    store = JsonlSessionStore(tmp_path / ".coda" / "sessions")
    store.create_session("session-old", model="test", agents_md_hash=None, initial_prompt_hash=None)
    import time  # noqa: PLC0415

    time.sleep(0.01)
    store.create_session("session-new", model="test", agents_md_hash=None, initial_prompt_hash=None)

    from coda.cli.resume import _resolve_session_id  # noqa: PLC0415

    resolved = _resolve_session_id(store, None)
    assert resolved == "session-new"


# ---------------------------------------------------------------------------
# 3. --list flag
# ---------------------------------------------------------------------------


def test_resume_list_flag_prints_recent(tmp_path: Path) -> None:
    """coda resume --list prints recent sessions without starting REPL."""
    from coda.memory.store import JsonlSessionStore  # noqa: PLC0415

    store = JsonlSessionStore(tmp_path / ".coda" / "sessions")
    for sid in ["s1", "s2", "s3"]:
        store.create_session(sid, model="test-model", agents_md_hash=None, initial_prompt_hash=None)

    # Use resume_command directly (simpler than routing through CliRunner subcommand)
    from coda.cli.resume import resume_command  # noqa: PLC0415
    import typer  # noqa: PLC0415
    from io import StringIO  # noqa: PLC0415
    import sys  # noqa: PLC0415

    with pytest.raises(typer.Exit) as exc_info:
        resume_command(
            root=tmp_path,
            list_recent=True,
            _workspace_root=tmp_path,
        )
    assert exc_info.value.exit_code == 0


def test_resume_list_empty_workspace(tmp_path: Path) -> None:
    """--list with no sessions prints a helpful message and exits 0."""
    from coda.cli.resume import resume_command  # noqa: PLC0415
    import typer  # noqa: PLC0415

    with pytest.raises(typer.Exit) as exc_info:
        resume_command(
            root=tmp_path,
            list_recent=True,
            _workspace_root=tmp_path,
        )
    assert exc_info.value.exit_code == 0


def test_resume_unknown_session_exits_1(tmp_path: Path) -> None:
    """Requesting a non-existent session ID exits with code 1."""
    from coda.cli.resume import resume_command  # noqa: PLC0415

    import typer  # noqa: PLC0415

    with pytest.raises(typer.Exit) as exc_info:
        resume_command(
            session_id="doesnotexist",
            root=tmp_path,
            _workspace_root=tmp_path,
        )
    assert exc_info.value.exit_code == 1
