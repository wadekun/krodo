"""Structured logging for Krodo — application log file + secret redactor.

configure_logging(workspace, session_id) sets up structlog to write:
  - human-readable output to stderr (for the terminal)
  - pure JSON-per-line to <workspace.root>/.krodo/logs/<session_id>.log

Note: session *events* (SessionEventLogger) are stored separately in
``<workspace.root>/.krodo/sessions/<session_id>.jsonl`` — a distinct file with
a different schema.  This separation prevents the mixed-file bug where
stdlib log prefixes (``INFO:krodo.session.abc:``) would break JSON parsing of
the event stream.

Secret redactor (stub for M1): replaces common API key patterns with
"[REDACTED]" so they never appear in log files.  M6 will expand the pattern
list to cover more providers.

Usage::

    logger = configure_logging(workspace, session_id)
    logger.info("tool_call", tool_name="read_file", path="src/main.py")
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import structlog

# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------

# Patterns for common API key formats (prefix-match style)
_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(sk-ant-[A-Za-z0-9_-]{10})[A-Za-z0-9_-]+"),  # Anthropic
    re.compile(r"(sk-[A-Za-z0-9]{10})[A-Za-z0-9]+"),  # OpenAI
    re.compile(r"(xoxb-[0-9]+-[0-9]+-[A-Za-z0-9]{4})[A-Za-z0-9]+"),  # Slack bot
    re.compile(r"(ghp_[A-Za-z0-9]{4})[A-Za-z0-9]+"),  # GitHub PAT
    re.compile(r"(gho_[A-Za-z0-9]{4})[A-Za-z0-9]+"),  # GitHub OAuth
]


def redact_secrets(text: str) -> str:
    """Replace recognised secret patterns with ``[REDACTED]``."""
    for pat in _SECRET_PATTERNS:
        text = pat.sub(r"\1[REDACTED]", text)
    return text


class _SecretRedactorProcessor:
    """structlog processor that redacts secrets from all string values."""

    def __call__(
        self,
        logger: Any,
        method: str,
        event_dict: dict[str, Any],
    ) -> dict[str, Any]:
        for key, value in list(event_dict.items()):
            if isinstance(value, str):
                event_dict[key] = redact_secrets(value)
        return event_dict


class _SecretRedactorFilter(logging.Filter):
    """stdlib logging Filter that redacts secrets from log record messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_secrets(str(record.msg))
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(
                    redact_secrets(a) if isinstance(a, str) else a for a in record.args
                )
            elif isinstance(record.args, dict):
                record.args = {
                    k: redact_secrets(v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
        return True


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------


_NOISY_LOGGERS = (
    "LiteLLM",
    "LiteLLM Proxy",
    "LiteLLM Router",
    "httpx",
    "httpcore",
    "openai",
    "anthropic",
)


def configure_logging(workspace: Any, session_id: str) -> logging.Logger:
    """Configure structlog and return a standard ``logging.Logger`` for the session.

    Parameters
    ----------
    workspace:
        The active Workspace (used to build the log file path).
    session_id:
        Unique session identifier; used as the log file name.
    """
    from krodo.core.workspace import Workspace

    ws: Workspace = workspace

    log_dir = ws.root / ".krodo" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    # .log extension (not .jsonl) to avoid confusion with session event files
    log_path = log_dir / f"{session_id}.log"

    # File handler — pure JSON-per-line (structlog JSONRenderer output).
    # Formatter must be "%(message)s" so the stdlib handler doesn't prepend
    # "INFO:krodo.session.abc:" before the JSON blob — that prefix would break
    # any downstream parser that expects clean JSON lines.
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(message)s"))
    file_handler.addFilter(_SecretRedactorFilter())

    # Stream handler — human-readable to stderr
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.WARNING)

    logging.basicConfig(
        handlers=[file_handler, stream_handler],
        level=logging.DEBUG,
        force=True,
    )

    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            _SecretRedactorProcessor(),  # type: ignore[list-item]
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Suppress verbose DEBUG output from LiteLLM and HTTP libraries.
    # Set KRODO_LOG_LEVEL=DEBUG in the environment to re-enable full traces.
    import os  # noqa: PLC0415

    debug_mode = os.environ.get("KRODO_LOG_LEVEL", "").upper() == "DEBUG"
    noisy_level = logging.DEBUG if debug_mode else logging.WARNING
    try:
        import litellm  # noqa: PLC0415

        litellm.suppress_debug_info = True
    except Exception:  # noqa: BLE001, S110
        pass
    for _name in _NOISY_LOGGERS:
        _nl = logging.getLogger(_name)
        _nl.setLevel(noisy_level)
        if not debug_mode:
            _nl.propagate = False

    logger = logging.getLogger(f"krodo.session.{session_id}")
    logger.info(
        '{"event": "session_start", "session_id": "%s", "workspace_root": "%s"}',
        session_id,
        str(ws.root),
    )

    return logger


def get_session_log_path(workspace: Any, session_id: str) -> Path:
    """Return the path to the structlog application log file for *session_id*.

    This is the ``.log`` file in ``.krodo/logs/`` — NOT the session event JSONL
    which lives in ``.krodo/sessions/<session_id>.jsonl``.
    """
    from krodo.core.workspace import Workspace

    ws: Workspace = workspace
    return ws.root / ".krodo" / "logs" / f"{session_id}.log"
