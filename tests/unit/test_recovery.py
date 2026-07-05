"""Unit tests for src/krodo/core/recovery.py — 7 recovery scenarios."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from krodo.core.recovery import (
    BAD_JSON,
    CONTEXT_LOSS,
    EACCES,
    PROVIDER_ERROR,
    SHA256_CONFLICT,
    STALL,
    TOOL_TIMEOUT,
    RecoveryAction,
    RecoveryContext,
    StallDetector,
    StallError,
    _signature,
    compute_sha256,
    handle,
)

# ---------------------------------------------------------------------------
# StallDetector
# ---------------------------------------------------------------------------


class TestStallDetector:
    def test_no_stall_on_first_call(self) -> None:
        det = StallDetector()
        det.record("edit_file", {"path": "foo.py"})  # no raise

    def test_no_stall_with_different_calls(self) -> None:
        det = StallDetector()
        det.record("edit_file", {"path": "foo.py"})
        det.record("edit_file", {"path": "bar.py"})
        det.record("edit_file", {"path": "baz.py"})  # different args — OK

    def test_stall_on_third_identical_write(self) -> None:
        det = StallDetector()
        args = {"path": "foo.py", "old_string": "x", "new_string": "y"}
        det.record("edit_file", args)
        det.record("edit_file", args)
        with pytest.raises(StallError):
            det.record("edit_file", args)

    def test_read_only_tools_do_not_count(self) -> None:
        det = StallDetector()
        for _ in range(10):
            det.record("read_file", {"path": "foo.py"})  # should never raise

    def test_reset_clears_state(self) -> None:
        det = StallDetector()
        args = {"path": "foo.py"}
        det.record("write_file", args)
        det.record("write_file", args)
        det.reset()
        # After reset, should be able to call twice more without stall
        det.record("write_file", args)
        det.record("write_file", args)

    def test_recent_calls_populated(self) -> None:
        det = StallDetector()
        det.record("write_file", {"path": "a.py"})
        assert len(det.recent_calls) == 1
        assert "write_file" in det.recent_calls[0]

    def test_signature_deterministic(self) -> None:
        sig1 = _signature("edit_file", {"a": 1, "b": 2})
        sig2 = _signature("edit_file", {"b": 2, "a": 1})
        assert sig1 == sig2  # sort_keys=True ensures determinism

    def test_read_only_intervention_resets_consecutive(self) -> None:
        """Read-only tool between two identical writes breaks the chain.

        Regression for session 5040d7bc: model called ``run_shell cat foo``
        twice with 30+ read_file / list_dir calls in between, then a third
        cat. Pre-fix this raised StallError because reads did not reset
        ``_consecutive``. Post-fix the chain is broken by the reads.
        """
        det = StallDetector()
        args = {"command": "cat foo.txt"}
        det.record("run_shell", args)  # consecutive=1
        det.record("run_shell", args)  # consecutive=2
        # Intervening read-only calls must reset the chain
        for _ in range(5):
            det.record("read_file", {"path": "x.py"})
        # Now a third identical run_shell should NOT raise — chain was broken
        det.record("run_shell", args)  # consecutive=1 (fresh chain)
        det.record("run_shell", args)  # consecutive=2

    def test_different_write_resets_consecutive(self) -> None:
        """A different write call also breaks the chain (different signature)."""
        det = StallDetector()
        det.record("write_file", {"path": "a.py", "content": "x"})
        det.record("write_file", {"path": "a.py", "content": "x"})
        # Different args → fresh chain
        det.record("write_file", {"path": "a.py", "content": "y"})
        det.record("write_file", {"path": "a.py", "content": "x"})
        det.record("write_file", {"path": "a.py", "content": "x"})
        # No stall — only 2 consecutive identical at the tail

    def test_truly_consecutive_writes_still_stall(self) -> None:
        """Sanity: 3 truly adjacent identical writes still raise StallError."""
        det = StallDetector()
        args = {"path": "a.py", "content": "x"}
        det.record("write_file", args)
        det.record("write_file", args)
        with pytest.raises(StallError):
            det.record("write_file", args)


# ---------------------------------------------------------------------------
# Scenario 1: BAD_JSON
# ---------------------------------------------------------------------------


class TestHandleBadJson:
    @pytest.mark.asyncio
    async def test_retry_on_first_occurrence(self) -> None:
        ctx = RecoveryContext(
            error_kind=BAD_JSON,
            exception=json.JSONDecodeError("bad", "", 0),
            retry_count=0,
            extra={"schema_hint": "read_file"},
        )
        action, msg = await handle(ctx)
        assert action == RecoveryAction.RETRY
        assert "JSON" in msg

    @pytest.mark.asyncio
    async def test_abort_after_two_retries(self) -> None:
        ctx = RecoveryContext(
            error_kind=BAD_JSON,
            exception=json.JSONDecodeError("bad", "", 0),
            retry_count=2,
        )
        action, msg = await handle(ctx)
        assert action == RecoveryAction.ABORT

    @pytest.mark.asyncio
    async def test_schema_hint_included_in_message(self) -> None:
        ctx = RecoveryContext(
            error_kind=BAD_JSON,
            retry_count=0,
            extra={"schema_hint": "my_tool_schema"},
        )
        _, msg = await handle(ctx)
        assert "my_tool_schema" in msg


# ---------------------------------------------------------------------------
# Scenario 2: TOOL_TIMEOUT
# ---------------------------------------------------------------------------


class TestHandleToolTimeout:
    @pytest.mark.asyncio
    async def test_skip_action(self) -> None:
        ctx = RecoveryContext(
            error_kind=TOOL_TIMEOUT,
            tool_name="run_shell",
            extra={"timeout_seconds": 30},
        )
        action, msg = await handle(ctx)
        assert action == RecoveryAction.SKIP
        assert "timed out" in msg
        assert "run_shell" in msg

    @pytest.mark.asyncio
    async def test_message_contains_timeout_value(self) -> None:
        ctx = RecoveryContext(
            error_kind=TOOL_TIMEOUT,
            tool_name="run_shell",
            extra={"timeout_seconds": 60},
        )
        _, msg = await handle(ctx)
        assert "60" in msg


# ---------------------------------------------------------------------------
# Scenario 3: STALL
# ---------------------------------------------------------------------------


class TestHandleStall:
    @pytest.mark.asyncio
    async def test_abort_action(self) -> None:
        ctx = RecoveryContext(
            error_kind=STALL,
            tool_name="edit_file",
            extra={"recent_calls": ["edit_file(foo)", "edit_file(foo)", "edit_file(foo)"]},
        )
        action, msg = await handle(ctx)
        assert action == RecoveryAction.ABORT
        assert "stall" in msg.lower()

    @pytest.mark.asyncio
    async def test_recent_calls_in_message(self) -> None:
        ctx = RecoveryContext(
            error_kind=STALL,
            tool_name="write_file",
            extra={"recent_calls": ["write_file(a)", "write_file(a)"]},
        )
        _, msg = await handle(ctx)
        assert "write_file" in msg


# ---------------------------------------------------------------------------
# Scenario 4: CONTEXT_LOSS
# ---------------------------------------------------------------------------


class TestHandleContextLoss:
    @pytest.mark.asyncio
    async def test_retry_action(self) -> None:
        ctx = RecoveryContext(
            error_kind=CONTEXT_LOSS,
            extra={"pinned_paths": ["src/main.py", "tests/test_main.py"]},
        )
        action, msg = await handle(ctx)
        assert action == RecoveryAction.RETRY
        assert "compressed" in msg.lower() or "budget" in msg.lower()

    @pytest.mark.asyncio
    async def test_pinned_paths_in_message(self) -> None:
        ctx = RecoveryContext(
            error_kind=CONTEXT_LOSS,
            extra={"pinned_paths": ["important_file.py"]},
        )
        _, msg = await handle(ctx)
        assert "important_file.py" in msg

    @pytest.mark.asyncio
    async def test_no_pinned_paths(self) -> None:
        ctx = RecoveryContext(error_kind=CONTEXT_LOSS)
        _, msg = await handle(ctx)
        assert "none" in msg.lower() or "compressed" in msg.lower()


# ---------------------------------------------------------------------------
# Scenario 5: SHA256_CONFLICT
# ---------------------------------------------------------------------------


class TestHandleSha256Conflict:
    @pytest.mark.asyncio
    async def test_skip_action(self) -> None:
        ctx = RecoveryContext(
            error_kind=SHA256_CONFLICT,
            extra={"path": "src/foo.py"},
        )
        action, msg = await handle(ctx)
        assert action == RecoveryAction.SKIP
        assert "modified externally" in msg or "external" in msg.lower()

    @pytest.mark.asyncio
    async def test_path_in_message(self) -> None:
        ctx = RecoveryContext(
            error_kind=SHA256_CONFLICT,
            extra={"path": "important/file.py"},
        )
        _, msg = await handle(ctx)
        assert "important/file.py" in msg


# ---------------------------------------------------------------------------
# Scenario 6: PROVIDER_ERROR (exp backoff)
# ---------------------------------------------------------------------------


class TestHandleProviderError:
    @pytest.mark.asyncio
    async def test_retry_on_first_error(self) -> None:
        ctx = RecoveryContext(
            error_kind=PROVIDER_ERROR,
            exception=RuntimeError("502 Bad Gateway"),
            retry_count=0,
        )
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            action, msg = await handle(ctx)
            mock_sleep.assert_called_once_with(1)
        assert action == RecoveryAction.RETRY

    @pytest.mark.asyncio
    async def test_backoff_increases(self) -> None:
        ctx = RecoveryContext(
            error_kind=PROVIDER_ERROR,
            exception=RuntimeError("503"),
            retry_count=1,
        )
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await handle(ctx)
            mock_sleep.assert_called_once_with(2)

    @pytest.mark.asyncio
    async def test_abort_after_three_retries(self) -> None:
        ctx = RecoveryContext(
            error_kind=PROVIDER_ERROR,
            exception=RuntimeError("504"),
            retry_count=3,
        )
        with patch("asyncio.sleep", new_callable=AsyncMock):
            action, _ = await handle(ctx)
        assert action == RecoveryAction.ABORT


# ---------------------------------------------------------------------------
# Scenario 7: EACCES
# ---------------------------------------------------------------------------


class TestHandleEacces:
    @pytest.mark.asyncio
    async def test_skip_action(self) -> None:
        ctx = RecoveryContext(
            error_kind=EACCES,
            exception=PermissionError("Permission denied"),
            extra={"path": "/etc/secret", "permission_bits": "0o444"},
        )
        action, msg = await handle(ctx)
        assert action == RecoveryAction.SKIP
        assert "Permission denied" in msg or "permission" in msg.lower()

    @pytest.mark.asyncio
    async def test_path_and_perm_in_message(self) -> None:
        ctx = RecoveryContext(
            error_kind=EACCES,
            extra={"path": "/restricted/file.txt", "permission_bits": "0o444"},
        )
        _, msg = await handle(ctx)
        assert "/restricted/file.txt" in msg
        assert "0o444" in msg


# ---------------------------------------------------------------------------
# Unknown error kind
# ---------------------------------------------------------------------------


class TestHandleUnknown:
    @pytest.mark.asyncio
    async def test_abort_for_unknown_kind(self) -> None:
        ctx = RecoveryContext(error_kind="space_laser_attack")
        action, msg = await handle(ctx)
        assert action == RecoveryAction.ABORT


# ---------------------------------------------------------------------------
# compute_sha256 helper
# ---------------------------------------------------------------------------


class TestComputeSha256:
    def test_returns_hex_string(self, tmp_path: Any) -> None:
        import pathlib

        p = pathlib.Path(tmp_path) / "test.txt"
        p.write_bytes(b"hello world")
        result = compute_sha256(p)
        assert result is not None
        assert len(result) == 64  # SHA-256 hex = 64 chars

    def test_returns_none_for_missing_file(self, tmp_path: Any) -> None:
        import pathlib

        result = compute_sha256(pathlib.Path(tmp_path) / "nonexistent.txt")
        assert result is None

    def test_different_content_different_hash(self, tmp_path: Any) -> None:
        import pathlib

        p1 = pathlib.Path(tmp_path) / "a.txt"
        p2 = pathlib.Path(tmp_path) / "b.txt"
        p1.write_bytes(b"hello")
        p2.write_bytes(b"world")
        assert compute_sha256(p1) != compute_sha256(p2)


# ---------------------------------------------------------------------------
# Type alias for pytest fixtures
# ---------------------------------------------------------------------------

from typing import Any  # noqa: E402 (after all imports for clarity)
