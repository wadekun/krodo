"""Tests for CostTracker, COST_SNAPSHOT emission, and the summary line (M6.2)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from krodo.cli.main import app
from krodo.core.events import SessionEventLogger
from krodo.core.loop import AgentLoop
from krodo.core.types import (
    Decision,
    Message,
    SessionEventType,
    ToolCall,
    ToolDef,
)
from krodo.core.workspace import LocalWorkspaceResolver
from krodo.obs.cost import CostTracker, format_token_count
from krodo.sandbox.firewall import LocalSandboxRunner
from krodo.tools.protocols import ToolContext
from krodo.tools.registry import ToolRegistry

# ---------------------------------------------------------------------------
# CostTracker unit tests
# ---------------------------------------------------------------------------


class TestCostTracker:
    def test_initial_state(self) -> None:
        tracker = CostTracker()
        assert tracker.prompt_tokens == 0
        assert tracker.completion_tokens == 0
        assert tracker.total_tokens == 0
        assert tracker.cost_usd is None

    def test_accumulates_usage(self) -> None:
        tracker = CostTracker()
        tracker.add({"prompt_tokens": 100, "completion_tokens": 50}, 0.01)
        tracker.add({"prompt_tokens": 200, "completion_tokens": 30}, 0.02)
        assert tracker.prompt_tokens == 300
        assert tracker.completion_tokens == 80
        assert tracker.total_tokens == 380
        assert tracker.cost_usd == pytest.approx(0.03)

    def test_cost_stays_none_when_unknown(self) -> None:
        tracker = CostTracker()
        tracker.add({"prompt_tokens": 10, "completion_tokens": 5}, None)
        assert tracker.total_tokens == 15
        assert tracker.cost_usd is None

    def test_none_usage_is_ignored(self) -> None:
        tracker = CostTracker()
        tracker.add(None, None)
        assert tracker.total_tokens == 0
        assert tracker.cost_usd is None


class TestFormatTokenCount:
    def test_small_counts_verbatim(self) -> None:
        assert format_token_count(950) == "950"
        assert format_token_count(0) == "0"

    def test_thousands_abbreviated(self) -> None:
        assert format_token_count(12345) == "12.3k"
        assert format_token_count(1000) == "1.0k"


# ---------------------------------------------------------------------------
# AgentLoop COST_SNAPSHOT emission
# ---------------------------------------------------------------------------


def _ctx(tmp_path: Path) -> ToolContext:
    ws = LocalWorkspaceResolver().resolve(explicit=tmp_path)
    sb = LocalSandboxRunner(ws)
    return ToolContext(
        workspace=ws, sandbox=sb, session_id="test", logger=logging.getLogger("test")
    )


class _AutoApprovalManager:
    @property
    def mode(self) -> str:
        return "full_auto"

    async def check(self, tool_call: ToolCall) -> Decision:
        return "allow"

    async def trust_session(self, session_id: str) -> None:
        pass


class _UsageProvider:
    """Scripted provider whose responses carry usage + cost."""

    name = "fake-usage"
    model = "fake/usage"

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

    def count_tokens(self, text: str) -> int:
        return 0

    def count_message_tokens(self, messages: list[Message]) -> int:
        return 0


class _CapturingEventLogger(SessionEventLogger):
    """In-memory event logger (no store) that records emitted events."""

    def __init__(self) -> None:
        super().__init__(session_id="test")
        self.events: list[tuple[SessionEventType, dict[str, Any]]] = []

    def emit(self, event_type, *, data=None, event_id=None):  # type: ignore[override]
        self.events.append((event_type, dict(data or {})))
        return super().emit(event_type, data=data, event_id=event_id)


@pytest.mark.asyncio
async def test_loop_emits_cost_snapshot_per_turn(tmp_path: Path) -> None:
    provider = _UsageProvider(
        [
            Message(
                role="assistant",
                content="hi",
                usage={"prompt_tokens": 100, "completion_tokens": 20},
                cost_usd=0.005,
            )
        ]
    )
    events = _CapturingEventLogger()
    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry(),
        tool_ctx=_ctx(tmp_path),
        approval=_AutoApprovalManager(),
        event_logger=events,
    )

    result = await loop.run("turn one")
    snapshots = [d for t, d in events.events if t == SessionEventType.COST_SNAPSHOT]
    assert len(snapshots) == 1
    assert snapshots[0]["turn_prompt_tokens"] == 100
    assert snapshots[0]["turn_completion_tokens"] == 20
    assert snapshots[0]["turn_cost_usd"] == pytest.approx(0.005)
    assert snapshots[0]["total_prompt_tokens"] == 100
    assert result.tokens_in == 100
    assert result.tokens_out == 20
    assert result.cost_usd == pytest.approx(0.005)

    # Second turn — totals are cumulative, turn values are per-turn.
    await loop.run("turn two")
    snapshots = [d for t, d in events.events if t == SessionEventType.COST_SNAPSHOT]
    assert len(snapshots) == 2
    assert snapshots[1]["turn_prompt_tokens"] == 100
    assert snapshots[1]["total_prompt_tokens"] == 200
    assert snapshots[1]["total_completion_tokens"] == 40
    assert snapshots[1]["total_cost_usd"] == pytest.approx(0.01)

    assert loop.cost_tracker.prompt_tokens == 200
    assert loop.cost_tracker.cost_usd == pytest.approx(0.01)


@pytest.mark.asyncio
async def test_loop_cost_snapshot_zero_when_no_usage(tmp_path: Path) -> None:
    """Providers without usage info still produce a (zeroed) snapshot."""
    provider = _UsageProvider([Message(role="assistant", content="hi")])
    events = _CapturingEventLogger()
    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry(),
        tool_ctx=_ctx(tmp_path),
        approval=_AutoApprovalManager(),
        event_logger=events,
    )
    result = await loop.run("hello")

    snapshots = [d for t, d in events.events if t == SessionEventType.COST_SNAPSHOT]
    assert len(snapshots) == 1
    assert snapshots[0]["turn_prompt_tokens"] == 0
    assert snapshots[0]["turn_cost_usd"] is None
    assert result.tokens_in == 0
    assert result.cost_usd is None


# ---------------------------------------------------------------------------
# E2E: summary line + COST_SNAPSHOT persisted in the session JSONL
# ---------------------------------------------------------------------------


def test_headless_summary_shows_tokens_and_cost(tmp_path: Path) -> None:
    runner = CliRunner()
    responses = [
        Message(
            role="assistant",
            content="done",
            usage={"prompt_tokens": 12345, "completion_tokens": 678},
            cost_usd=0.0231,
        )
    ]

    with patch(
        "krodo.cli.main.LiteLLMProvider",
        return_value=_UsageProvider(responses),
    ):
        result = runner.invoke(
            app,
            ["--root", str(tmp_path), "--approval", "full_auto", "do it"],
        )

    assert result.exit_code == 0, result.output
    assert "tokens     : 12.3k in / 678 out" in result.output
    assert "cost $0.0231" in result.output

    # COST_SNAPSHOT persisted in the session JSONL
    session_files = list((tmp_path / ".krodo" / "sessions").glob("*.jsonl"))
    assert len(session_files) == 1
    types = [
        json.loads(line)["type"]
        for line in session_files[0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert "cost_snapshot" in types


def test_headless_summary_omits_cost_line_without_usage(tmp_path: Path) -> None:
    runner = CliRunner()
    responses = [Message(role="assistant", content="done")]

    with patch(
        "krodo.cli.main.LiteLLMProvider",
        return_value=_UsageProvider(responses),
    ):
        result = runner.invoke(
            app,
            ["--root", str(tmp_path), "--approval", "full_auto", "do it"],
        )

    assert result.exit_code == 0, result.output
    assert "tokens     :" not in result.output
