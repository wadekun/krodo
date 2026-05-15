"""Tests for coda.obs.logger — JSONL writing + secret redactor."""

from __future__ import annotations

from pathlib import Path

import pytest

from coda.core.workspace import LocalWorkspaceResolver
from coda.obs.logger import (
    configure_logging,
    get_session_log_path,
    redact_secrets,
)

# ---------------------------------------------------------------------------
# Secret redactor
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "secret",
    [
        "sk-ant-api03-ABCDEF1234567890abcdef",  # Anthropic
        "sk-ABCDEF1234567890abcdef",  # OpenAI
        "xoxb-123456789-987654321-ABCDabcd",  # Slack
        "ghp_ABCDabcd1234",  # GitHub PAT
        "gho_ABCDabcd1234",  # GitHub OAuth
    ],
)
def test_redact_secrets_matches(secret: str) -> None:
    result = redact_secrets(f"key={secret}")
    assert "[REDACTED]" in result
    # The full secret should not appear verbatim
    assert secret not in result


def test_redact_secrets_preserves_safe_text() -> None:
    safe = "hello world — no secrets here"
    assert redact_secrets(safe) == safe


def test_redact_multiple_secrets_in_one_string() -> None:
    text = "key1=sk-ant-api03-ABCDEF1234567890 key2=ghp_ABCDabcd5678"
    result = redact_secrets(text)
    assert result.count("[REDACTED]") >= 2


# ---------------------------------------------------------------------------
# configure_logging — log file creation
# ---------------------------------------------------------------------------


def test_configure_logging_creates_log_file(tmp_path: Path) -> None:
    ws = LocalWorkspaceResolver().resolve(explicit=tmp_path)
    configure_logging(ws, "test-session-001")
    log_path = get_session_log_path(ws, "test-session-001")
    assert log_path.exists()
    assert log_path.suffix == ".jsonl"


def test_configure_logging_returns_logger(tmp_path: Path) -> None:
    import logging

    ws = LocalWorkspaceResolver().resolve(explicit=tmp_path)
    logger = configure_logging(ws, "test-session-002")
    assert isinstance(logger, logging.Logger)


def test_log_file_written_to_coda_logs_dir(tmp_path: Path) -> None:
    ws = LocalWorkspaceResolver().resolve(explicit=tmp_path)
    configure_logging(ws, "test-session-003")
    log_dir = tmp_path / ".coda" / "logs"
    assert log_dir.is_dir()
    assert (log_dir / "test-session-003.jsonl").exists()


def test_log_entry_written(tmp_path: Path) -> None:
    ws = LocalWorkspaceResolver().resolve(explicit=tmp_path)
    logger = configure_logging(ws, "test-session-004")
    logger.info("tool_call: read_file path=foo.py")
    log_path = get_session_log_path(ws, "test-session-004")
    content = log_path.read_text(encoding="utf-8")
    assert len(content) > 0


def test_secret_not_written_to_log(tmp_path: Path) -> None:
    ws = LocalWorkspaceResolver().resolve(explicit=tmp_path)
    logger = configure_logging(ws, "test-session-005")
    # Simulate logging a message that contains an API key
    secret = "sk-ant-api03-SUPERSECRET1234567890"
    logger.info("api_call key=%s", secret)
    log_path = get_session_log_path(ws, "test-session-005")
    content = log_path.read_text(encoding="utf-8")
    # The full secret must not appear in the log
    assert "SUPERSECRET1234567890" not in content


# ---------------------------------------------------------------------------
# get_session_log_path
# ---------------------------------------------------------------------------


def test_get_session_log_path_correct_location(tmp_path: Path) -> None:
    ws = LocalWorkspaceResolver().resolve(explicit=tmp_path)
    path = get_session_log_path(ws, "abc-123")
    assert path == tmp_path / ".coda" / "logs" / "abc-123.jsonl"


# ---------------------------------------------------------------------------
# _SecretRedactorProcessor (structlog processor)
# ---------------------------------------------------------------------------


def test_secret_redactor_processor_redacts_string_values(tmp_path: Path) -> None:
    from coda.obs.logger import _SecretRedactorProcessor

    proc = _SecretRedactorProcessor()
    event_dict = {"event": "test", "key": "sk-ant-api03-ABC123456789abc"}
    result = proc(None, "info", event_dict)
    assert "[REDACTED]" in result["key"]
    assert "ABC123456789abc" not in result["key"]


def test_secret_redactor_processor_ignores_non_strings(tmp_path: Path) -> None:
    from coda.obs.logger import _SecretRedactorProcessor

    proc = _SecretRedactorProcessor()
    event_dict = {"count": 42, "flag": True, "event": "safe"}
    result = proc(None, "info", event_dict)
    assert result["count"] == 42
    assert result["flag"] is True


# ---------------------------------------------------------------------------
# _SecretRedactorFilter — dict args branch
# ---------------------------------------------------------------------------


def test_secret_not_written_with_dict_args(tmp_path: Path) -> None:
    ws = LocalWorkspaceResolver().resolve(explicit=tmp_path)
    logger = configure_logging(ws, "test-session-006")
    secret = "sk-ant-api03-DICTSECRET9876543"
    # Pass args as dict (format string with %(key)s)
    logger.info("api key is %(key)s", {"key": secret})
    log_path = get_session_log_path(ws, "test-session-006")
    content = log_path.read_text(encoding="utf-8")
    assert "DICTSECRET9876543" not in content
