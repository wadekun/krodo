"""Unit tests for src/coda/obs/logger.py — M4.5 additions."""

from __future__ import annotations

import logging
from pathlib import Path

from coda.obs.logger import configure_logging, get_session_log_path, redact_secrets

# ---------------------------------------------------------------------------
# redact_secrets
# ---------------------------------------------------------------------------


class TestRedactSecrets:
    def test_anthropic_key(self) -> None:
        text = "key=sk-ant-api03-ABCDEFGHIJ1234567890"
        result = redact_secrets(text)
        assert "[REDACTED]" in result
        assert "ABCDEFGHIJ" not in result

    def test_openai_key(self) -> None:
        text = "sk-ABCDEFGHIJKLMNOPQRST"
        result = redact_secrets(text)
        assert "[REDACTED]" in result

    def test_no_secret(self) -> None:
        text = "hello world"
        assert redact_secrets(text) == text


# ---------------------------------------------------------------------------
# configure_logging — noisy-logger suppression (M4.5)
# ---------------------------------------------------------------------------


class TestConfigureLogging:
    def test_litellm_logger_clamped_to_warning(self, tmp_path: Path) -> None:
        """After configure_logging(), the 'LiteLLM' logger must be WARNING or higher."""
        from coda.core.workspace import LocalWorkspaceResolver  # noqa: PLC0415

        ws = LocalWorkspaceResolver().resolve(explicit=tmp_path)
        configure_logging(ws, "test-session-warn")

        litellm_logger = logging.getLogger("LiteLLM")
        assert litellm_logger.level >= logging.WARNING, (
            f"Expected LiteLLM logger level >= WARNING ({logging.WARNING}), "
            f"got {litellm_logger.level}"
        )

    def test_httpx_logger_clamped(self, tmp_path: Path) -> None:
        from coda.core.workspace import LocalWorkspaceResolver  # noqa: PLC0415

        ws = LocalWorkspaceResolver().resolve(explicit=tmp_path)
        configure_logging(ws, "test-session-httpx")

        httpx_logger = logging.getLogger("httpx")
        assert httpx_logger.level >= logging.WARNING

    def test_returns_stdlib_logger(self, tmp_path: Path) -> None:
        from coda.core.workspace import LocalWorkspaceResolver  # noqa: PLC0415

        ws = LocalWorkspaceResolver().resolve(explicit=tmp_path)
        logger = configure_logging(ws, "test-session-type")
        assert isinstance(logger, logging.Logger)

    def test_log_file_created(self, tmp_path: Path) -> None:
        from coda.core.workspace import LocalWorkspaceResolver  # noqa: PLC0415

        ws = LocalWorkspaceResolver().resolve(explicit=tmp_path)
        configure_logging(ws, "test-session-file")
        log_path = get_session_log_path(ws, "test-session-file")
        assert log_path.exists()


# ---------------------------------------------------------------------------
# get_session_log_path
# ---------------------------------------------------------------------------


class TestGetSessionLogPath:
    def test_returns_path_under_coda_logs(self, tmp_path: Path) -> None:
        from coda.core.workspace import LocalWorkspaceResolver  # noqa: PLC0415

        ws = LocalWorkspaceResolver().resolve(explicit=tmp_path)
        path = get_session_log_path(ws, "my-session")
        assert path == ws.root / ".coda" / "logs" / "my-session.jsonl"
