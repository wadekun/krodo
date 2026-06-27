"""JSONL-backed SessionStore — persistent session event storage (M5.1).

Each session maps to a single ``<sessions_dir>/<session_id>.jsonl`` file.
The **first line** of every file is always a ``SESSION_INIT`` event that acts
as a queryable header, allowing ``list_recent`` to read just one line per file
(O(N sessions), not O(N events)).

Usage::

    store = JsonlSessionStore(workspace.root / ".krodo" / "sessions")
    store.create_session(session_id, model="anthropic/claude-3-5-sonnet-20241022")
    store.append_event(event)
    events = store.load_events(session_id)   # sorted by seq
    rows = store.list_recent(limit=10)       # newest-first SessionRow list

Protocol design (§3.4):
    A ``SessionStore`` Protocol defines the interface so future backends
    (e.g. ``SqliteSessionStore``) can be swapped in without touching callers.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from krodo.core.types import SessionEvent, SessionEventType

# ---------------------------------------------------------------------------
# Data objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionRow:
    """Summary row returned by :meth:`SessionStore.list_recent`.

    Populated from the ``SESSION_INIT`` header of each session file, so no
    full scan is required.
    """

    session_id: str
    created_at: datetime
    last_updated_at: datetime
    model: str | None
    first_user_prompt: str | None  # truncated to 80 chars; None until M5.2


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class SessionStore(Protocol):
    """Storage interface for session events.

    Implementations must be append-safe and support cross-process access
    (multiple processes appending to the same session are supported by the
    JSONL backend because OS filesystem append is atomic for small writes).
    """

    def create_session(
        self,
        session_id: str,
        *,
        model: str | None,
        agents_md_hash: str | None,
        initial_prompt_hash: str | None,
    ) -> None:
        """Create a new session, writing a SESSION_INIT header event (seq=0)."""
        ...

    def append_event(self, event: SessionEvent) -> None:
        """Append a single event to the session. Thread-unsafe by design."""
        ...

    def load_events(self, session_id: str) -> list[SessionEvent]:
        """Return all events for *session_id* sorted by seq.

        Corrupt or non-JSON lines are silently skipped.
        """
        ...

    def max_seq(self, session_id: str) -> int:
        """Return the highest ``seq`` value in the session, or ``-1`` if empty."""
        ...

    def list_recent(self, *, limit: int = 10) -> list[SessionRow]:
        """Return up to *limit* sessions sorted newest-first by ``created_at``."""
        ...


# ---------------------------------------------------------------------------
# JSONL implementation
# ---------------------------------------------------------------------------


class JsonlSessionStore:
    """Backed by ``<sessions_dir>/<session_id>.jsonl`` files.

    First-line convention: every session file begins with a ``SESSION_INIT``
    event.  This lets :meth:`list_recent` scan only the first line of each
    file for fast listing without a database.

    ``max_seq`` reads the **last** non-empty line of the file; this is O(1)
    for typical JSON events (< 4 KiB) via a reverse seek.
    """

    def __init__(self, sessions_dir: Path) -> None:
        self._dir = sessions_dir

    # ------------------------------------------------------------------
    # SessionStore protocol implementation
    # ------------------------------------------------------------------

    def create_session(
        self,
        session_id: str,
        *,
        model: str | None = None,
        agents_md_hash: str | None = None,
        initial_prompt_hash: str | None = None,
        **extra_data: Any,
    ) -> None:
        """Write the SESSION_INIT header as the first line (seq=0)."""
        krodo_version = _get_krodo_version()
        data: dict[str, Any] = {
            "model": model,
            "agents_md_hash": agents_md_hash,
            "initial_prompt_hash": initial_prompt_hash,
            "krodo_version": krodo_version,
        }
        data.update(extra_data)

        event = SessionEvent(
            id=str(uuid.uuid4()),
            session_id=session_id,
            seq=0,
            type=SessionEventType.SESSION_INIT,
            timestamp=datetime.now(UTC),
            data=data,
        )

        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path(session_id)
        # "w" to start fresh — this is a new session
        with path.open("w", encoding="utf-8") as fh:
            fh.write(event.model_dump_json() + "\n")

    def append_event(self, event: SessionEvent) -> None:
        """Append *event* as a JSONL line. Silently absorbs OS errors."""
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path(event.session_id)
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(event.model_dump_json() + "\n")
        except OSError:
            pass

    def load_events(self, session_id: str) -> list[SessionEvent]:
        """Load and return all events sorted by seq. Skips corrupt lines."""
        path = self._path(session_id)
        if not path.exists():
            return []

        events: list[SessionEvent] = []
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                events.append(SessionEvent.model_validate_json(raw))
            except Exception:  # noqa: BLE001, S112
                continue

        events.sort(key=lambda e: e.seq)
        return events

    def max_seq(self, session_id: str) -> int:
        """Return the highest seq in the file, or -1 if the file is empty / missing."""
        path = self._path(session_id)
        if not path.exists():
            return -1
        last = _read_last_nonempty_line(path)
        if last is None:
            return -1
        try:
            obj = json.loads(last)
            return int(obj.get("seq", -1))
        except (json.JSONDecodeError, ValueError, TypeError):
            return -1

    def list_recent(self, *, limit: int = 10) -> list[SessionRow]:
        """Return up to *limit* sessions newest-first.

        Reads only the first line (SESSION_INIT header) from each file.
        Files whose first line is not a SESSION_INIT event are silently
        skipped (e.g., corrupted files or files from older formats).
        """
        if not self._dir.exists():
            return []

        rows: list[SessionRow] = []
        for path in self._dir.glob("*.jsonl"):
            row = _parse_session_row(path)
            if row is not None:
                rows.append(row)

        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows[:limit]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _path(self, session_id: str) -> Path:
        return self._dir / f"{session_id}.jsonl"


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _read_last_nonempty_line(path: Path) -> str | None:
    """Read the last non-empty line of *path* using a reverse seek.

    Reads at most 8 KiB from the end — enough for any valid JSON event line.
    Falls back to a full scan if the file is smaller than the chunk size.
    """
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)  # SEEK_END
            size = fh.tell()
            if size == 0:
                return None
            chunk_size = min(8192, size)
            fh.seek(-chunk_size, 2)
            chunk = fh.read().decode("utf-8", errors="replace")
        lines = [ln for ln in chunk.splitlines() if ln.strip()]
        return lines[-1] if lines else None
    except OSError:
        return None


def _parse_session_row(path: Path) -> SessionRow | None:
    """Return a :class:`SessionRow` from the SESSION_INIT header of *path*.

    Returns ``None`` if the first line is absent, not valid JSON, or not a
    SESSION_INIT event.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            first_line = fh.readline().strip()
        if not first_line:
            return None

        obj = json.loads(first_line)
        if obj.get("type") != "session_init":
            return None

        ts_str = obj.get("timestamp", "")
        created_at = datetime.fromisoformat(ts_str)

        data: dict[str, Any] = obj.get("data", {})

        # last_updated_at: read the last non-empty line for its timestamp
        last_updated_at = created_at
        last_line = _read_last_nonempty_line(path)
        if last_line and last_line != first_line:
            try:
                last_obj = json.loads(last_line)
                raw_ts = last_obj.get("timestamp", "")
                if raw_ts:
                    last_updated_at = datetime.fromisoformat(raw_ts)
            except Exception:  # noqa: BLE001, S110
                pass

        return SessionRow(
            session_id=obj.get("session_id", path.stem),
            created_at=created_at,
            last_updated_at=last_updated_at,
            model=data.get("model"),
            first_user_prompt=None,  # populated lazily in M5.2 list display
        )
    except Exception:  # noqa: BLE001
        return None


def _get_krodo_version() -> str:
    try:
        from importlib.metadata import version  # noqa: PLC0415

        return version("krodo")
    except Exception:  # noqa: BLE001
        return "dev"
