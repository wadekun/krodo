"""Tests for M9-closeout indexer.close() lifecycle wiring (review H).

Covers the three call sites added alongside the canary defense:
``_run_headless`` end-of-turn, and the two exit paths inside
``repl_session_cycle`` (normal REPL exit, and right before a ``:resume``
rebuild swaps in a new ``SessionComponents``). All three are None-guarded so
``symbol_backend: off`` sessions (``indexer is None``) are unaffected.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from krodo.cli.main import SessionComponents, _run_headless
from krodo.cli.resume import repl_session_cycle
from krodo.core.loop import TurnResult
from krodo.obs.cost import CostTracker


def _components(tmp_path: Path, *, indexer: object | None) -> SessionComponents:
    loop = MagicMock()
    loop.run = AsyncMock(return_value=TurnResult(final_text="ok"))
    return SessionComponents(
        workspace=MagicMock(root=tmp_path),
        loop=loop,
        logger=MagicMock(),
        session_id="s1",
        event_logger=MagicMock(),
        store=MagicMock(),
        sessions_path=tmp_path / "s1.jsonl",
        log_path=tmp_path / "s1.log",
        max_tokens=1024,
        cost_tracker=CostTracker(),
        approval=MagicMock(),
        indexer=indexer,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# _run_headless
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_headless_closes_indexer(tmp_path: Path) -> None:
    indexer = MagicMock()
    components = _components(tmp_path, indexer=indexer)

    await _run_headless("do something", components)

    indexer.close.assert_called_once()


@pytest.mark.asyncio
async def test_run_headless_none_indexer_is_noop(tmp_path: Path) -> None:
    components = _components(tmp_path, indexer=None)

    # Must not raise even though there is no indexer to close.
    await _run_headless("do something", components)


# ---------------------------------------------------------------------------
# repl_session_cycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repl_session_cycle_closes_indexer_on_normal_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    indexer = MagicMock()
    components = _components(tmp_path, indexer=indexer)

    monkeypatch.setattr("krodo.cli.repl.run_repl", AsyncMock(return_value=None), raising=False)

    async def _rebuild(_session_id: str) -> SessionComponents:
        raise AssertionError("rebuild should not be called — run_repl returned None")

    await repl_session_cycle(components, _rebuild)  # type: ignore[arg-type]

    indexer.close.assert_called_once()


@pytest.mark.asyncio
async def test_repl_session_cycle_closes_indexer_before_resume_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_indexer = MagicMock()
    new_indexer = MagicMock()
    components = _components(tmp_path, indexer=old_indexer)
    resumed = _components(tmp_path, indexer=new_indexer)

    run_repl_mock = AsyncMock(side_effect=["target-session", None])
    monkeypatch.setattr("krodo.cli.repl.run_repl", run_repl_mock, raising=False)
    monkeypatch.setattr(
        "krodo.cli.resume.build_resumed_components",
        MagicMock(return_value=resumed),
    )

    async def _rebuild(_session_id: str) -> SessionComponents:
        return resumed

    await repl_session_cycle(components, _rebuild)  # type: ignore[arg-type]

    old_indexer.close.assert_called_once()
    new_indexer.close.assert_called_once()


@pytest.mark.asyncio
async def test_repl_session_cycle_none_indexer_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    components = _components(tmp_path, indexer=None)
    monkeypatch.setattr("krodo.cli.repl.run_repl", AsyncMock(return_value=None), raising=False)

    async def _rebuild(_session_id: str) -> SessionComponents:
        raise AssertionError("rebuild should not be called")

    # Must not raise even though there is no indexer to close.
    await repl_session_cycle(components, _rebuild)  # type: ignore[arg-type]
