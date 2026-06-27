"""SessionEventLogger — structured JSONL event emitter (M3, updated M5.1).

Wraps a ``SessionStore`` (or an optional fallback ``jsonl_path``) to provide
a typed, sequenced interface for emitting SessionEvent records at key
agent-loop boundaries.

Usage::

    store = JsonlSessionStore(sessions_dir)
    logger = SessionEventLogger.from_store(store, session_id="abc123")
    logger.emit(SessionEventType.USER_MESSAGE, data={"content": "..."})

Cross-process seq correctness (M5.1 fix):
    ``from_store`` bootstraps ``self._seq`` by calling
    ``store.max_seq(session_id) + 1``, so a new logger instance that appends
    to an existing session (e.g. ``krodo resume``, ``krodo undo``) will never
    repeat a ``seq`` value.

The ``seq`` counter is not thread-safe by design (the agent loop is
single-threaded async).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from krodo.core.types import SessionEvent, SessionEventType

if TYPE_CHECKING:
    from krodo.memory.store import SessionStore


class SessionEventLogger:
    """Emit typed, sequenced SessionEvents to a SessionStore sink.

    Parameters
    ----------
    session_id:
        Unique identifier for the current session.  Embedded in every event.
    store:
        ``SessionStore`` implementation used for persistence.  When ``None``,
        events are only forwarded to the stdlib *logger* at DEBUG level (useful
        for unit tests that don't need file I/O).
    jsonl_path:
        Deprecated legacy parameter — used only for the ``krodo undo``
        cross-process appends that run outside the normal session lifecycle.
        Prefer ``from_store`` for new call sites.
    logger:
        stdlib logger for debug-level event tracing and error reporting.
    """

    def __init__(
        self,
        session_id: str,
        *,
        store: SessionStore | None = None,
        jsonl_path: Path | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._session_id = session_id
        self._store = store
        self._jsonl_path = jsonl_path  # legacy fallback
        self._logger = logger or logging.getLogger(__name__)

        # Bootstrap seq from existing storage to prevent duplicates on resume.
        if store is not None:
            self._seq = store.max_seq(session_id) + 1
        else:
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
        """Emit a single SessionEvent and persist it to the store.

        Parameters
        ----------
        event_type:
            One of the SessionEventType enum values.
        data:
            Arbitrary payload dict.  Must be JSON-serialisable.
        event_id:
            Optional explicit UUID string.  Auto-generated if omitted.

        Returns the emitted SessionEvent.
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
        """Re-emit a SessionEvent created elsewhere (e.g. by a Compressor).

        Overwrites the event's session_id and seq to maintain sequence
        integrity.  Returns the updated event.
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
        """Persist *event* via the store (preferred) or legacy jsonl_path."""
        self._logger.debug("session_event type=%s seq=%d", event.type, event.seq)

        if self._store is not None:
            self._store.append_event(event)
            return

        # Legacy path: direct JSONL append (used by krodo undo cross-process)
        if self._jsonl_path is None:
            return
        line = event.model_dump_json() + "\n"
        try:
            self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            with self._jsonl_path.open("a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError as exc:
            self._logger.warning("Failed to write session event to %s: %s", self._jsonl_path, exc)

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

    @classmethod
    def from_store(
        cls,
        store: SessionStore,
        session_id: str,
        *,
        logger: logging.Logger | None = None,
    ) -> SessionEventLogger:
        """Preferred factory: create a logger backed by a SessionStore.

        Bootstraps ``_seq`` from ``store.max_seq(session_id) + 1`` so that
        cross-process appends (resume, undo) never repeat a seq value.
        """
        return cls(session_id=session_id, store=store, logger=logger)

    @classmethod
    def from_workspace_path(
        cls,
        session_id: str,
        workspace_root: Path,
        *,
        logger: logging.Logger | None = None,
    ) -> SessionEventLogger:
        """Deprecated: creates a logger using a legacy direct jsonl_path.

        Kept for backward-compatibility with older undo tests.  New callers
        should use ``from_store`` instead.

        Path: ``<workspace_root>/.krodo/sessions/<session_id>.jsonl``
        """
        jsonl_path = workspace_root / ".krodo" / "sessions" / f"{session_id}.jsonl"
        return cls(session_id=session_id, jsonl_path=jsonl_path, logger=logger)
