"""SessionEventLogger — structured JSONL event emitter (M3).

Wraps the existing JSONL logger to provide a typed, sequenced interface for
emitting SessionEvent records at key agent-loop boundaries.

All events are written to the same JSONL file used by configure_logging()
(``<workspace>/.coda/logs/<session_id>.jsonl``) so that cost dashboards,
replay tools, and tracing backends (Langfuse) can consume a single stream.

Usage::

    logger = SessionEventLogger(session_id="abc123", jsonl_logger=log)
    await logger.emit(SessionEventType.USER_MESSAGE, data={"content": "..."})
    await logger.emit(SessionEventType.COMPRESSION, data={"strategy": "llm"})

The ``seq`` counter is monotonically increasing and is not thread-safe by
design (the agent loop is single-threaded async).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from coda.core.types import SessionEvent, SessionEventType


class SessionEventLogger:
    """Emit typed, sequenced SessionEvents to a JSONL sink.

    Parameters
    ----------
    session_id:
        Unique identifier for the current session.  Embedded in every event.
    jsonl_path:
        Path to the JSONL file to write events to.  Created on first emit.
        If None, events are only passed to the stdlib *logger* at DEBUG level.
    logger:
        stdlib logger used for debug-level event logging when no JSONL path
        is set, or for error logging if JSONL writes fail.
    """

    def __init__(
        self,
        session_id: str,
        *,
        jsonl_path: Path | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._session_id = session_id
        self._jsonl_path = jsonl_path
        self._logger = logger or logging.getLogger(__name__)
        self._seq = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def emit(
        self,
        event_type: SessionEventType,
        *,
        data: dict[str, Any] | None = None,
        event_id: str | None = None,
    ) -> SessionEvent:
        """Emit a single SessionEvent and write it to the JSONL sink.

        Parameters
        ----------
        event_type:
            One of the SessionEventType enum values.
        data:
            Arbitrary payload dict.  Must be JSON-serialisable.
        event_id:
            Optional explicit UUID string.  Auto-generated if omitted.

        Returns the emitted SessionEvent (useful for tests and for passing to
        context_manager as a compression_event payload).
        """
        event = SessionEvent(
            id=event_id or str(uuid.uuid4()),
            session_id=self._session_id,
            seq=self._seq,
            type=event_type,
            timestamp=datetime.now(UTC),
            data=data or {},
        )
        self._seq += 1

        self._write(event)
        return event

    def emit_from(self, event: SessionEvent) -> SessionEvent:
        """Re-emit a SessionEvent that was created elsewhere (e.g. by a Compressor).

        Overwrites the event's session_id and seq to maintain sequence integrity.
        Returns the updated event.
        """
        updated = SessionEvent(
            id=event.id,
            session_id=self._session_id,
            seq=self._seq,
            type=event.type,
            timestamp=event.timestamp,
            data=event.data,
        )
        self._seq += 1
        self._write(updated)
        return updated

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def next_seq(self) -> int:
        return self._seq

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _write(self, event: SessionEvent) -> None:
        """Write *event* as a JSONL line.  Logs a warning on I/O failure."""
        line = event.model_dump_json() + "\n"

        self._logger.debug("session_event type=%s seq=%d", event.type, event.seq)

        if self._jsonl_path is None:
            return

        try:
            self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            with self._jsonl_path.open("a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError as exc:
            self._logger.warning("Failed to write session event to %s: %s", self._jsonl_path, exc)

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_workspace_path(
        cls,
        session_id: str,
        workspace_root: Path,
        *,
        logger: logging.Logger | None = None,
    ) -> SessionEventLogger:
        """Create a SessionEventLogger that writes to the standard JSONL path.

        Path: ``<workspace_root>/.coda/logs/<session_id>.jsonl``
        """
        jsonl_path = workspace_root / ".coda" / "logs" / f"{session_id}.jsonl"
        return cls(session_id=session_id, jsonl_path=jsonl_path, logger=logger)
