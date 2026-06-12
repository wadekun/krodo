"""Session replay — reconstruct conversation history from stored events (M5.2).

``replay_events`` walks a list of ``SessionEvent`` records and reconstructs the
``InMemoryContextManager`` history so a resumed session feels like a continuation
of the previous one.

Mapping table (see M5.2 spec):
  USER_MESSAGE      → ctx.add_user_input(data["content"])
  ASSISTANT_MESSAGE → ctx.append_assistant(Message(...))
  TOOL_RESULT       → ctx.append_tool_result(ToolResult(...))
  COMPRESSION       → replace preceding replayed messages with the summary block
  TOOL_CALL         → skip (already embedded in ASSISTANT_MESSAGE data)
  APPROVAL_DECISION → re-apply the latest trust ``state`` snapshot to the
                      approval manager when one is passed (M6.5)
  CHECKPOINT / UNDO / ERROR / SESSION_INIT / COST_SNAPSHOT
                    → skip (metadata only)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from coda.core.types import Message, SessionEventType, ToolResult

if TYPE_CHECKING:
    from coda.core.context import InMemoryContextManager
    from coda.core.types import SessionEvent
    from coda.sandbox.approval import TerminalApprovalManager


# ---------------------------------------------------------------------------
# ReplayStats
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayStats:
    """Summary of what was replayed."""

    turns: int            # number of complete user-assistant exchanges
    messages_restored: int  # total messages appended to history
    compressed: bool      # True if at least one COMPRESSION event was encountered


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def replay_events(
    events: list[SessionEvent],
    ctx: InMemoryContextManager,
    approval: TerminalApprovalManager | None = None,
) -> ReplayStats:
    """Reconstruct *ctx._history* from *events*.

    Events are processed in ``seq`` order.  The context manager is mutated
    in place; existing history is left intact (allows pre-pending
    ``<project_memory>`` before calling replay).

    When *approval* is provided, the latest ``APPROVAL_DECISION`` event
    carrying a trust ``state`` snapshot is re-applied via
    ``approval.restore_state(...)`` so session/pattern trust survives resume.

    Returns :class:`ReplayStats` with counts for banner display.
    """
    # Sort by seq for deterministic replay (load_events already does this,
    # but defensive sort here costs O(N log N) and protects callers that pass
    # unsorted lists).
    sorted_events = sorted(events, key=lambda e: e.seq)

    turns = 0
    messages_restored = 0
    compressed = False
    last_approval_state: dict | None = None

    # Healing for interrupted sessions: tool_use ids awaiting a tool_result.
    # Sessions written before the dangling-tool_use fix (or killed mid-batch)
    # may persist an assistant message whose tool calls never got results —
    # Anthropic rejects such histories, so we synthesize "[skipped]" results
    # whenever a non-tool_result event (or the end of the stream) follows.
    pending_tool_ids: list[str] = []

    def _flush_pending() -> None:
        nonlocal messages_restored
        for tc_id in pending_tool_ids:
            ctx.append_tool_result(
                ToolResult(
                    tool_call_id=tc_id,
                    content="[skipped: interrupted before execution]",
                    is_error=True,
                )
            )
            messages_restored += 1
        pending_tool_ids.clear()

    for event in sorted_events:
        et = event.type
        data = event.data

        if et == SessionEventType.SESSION_INIT:
            # Header event — no history to replay
            continue

        elif et == SessionEventType.USER_MESSAGE:
            _flush_pending()
            content = str(data.get("content", ""))
            if content:
                ctx.add_user_input(content)
                messages_restored += 1
                turns += 1

        elif et == SessionEventType.ASSISTANT_MESSAGE:
            _flush_pending()
            content = data.get("content", "")
            tool_calls_raw = data.get("tool_calls")
            from coda.core.types import ToolCall  # noqa: PLC0415

            tool_calls = None
            if tool_calls_raw and isinstance(tool_calls_raw, list):
                try:
                    # Build leniently: persisted events may omit `arguments`
                    # (older sessions stored only name+id). Default missing
                    # arguments to {} so the tool_use/tool_result pairing (keyed
                    # by id) survives replay and is re-sent correctly to the LLM.
                    tool_calls = [
                        ToolCall(
                            id=str(tc.get("id", "")),
                            name=str(tc.get("name", "")),
                            arguments=tc.get("arguments", {}) or {},
                        )
                        for tc in tool_calls_raw
                        if isinstance(tc, dict)
                    ]
                except Exception:  # noqa: BLE001
                    tool_calls = None

            msg = Message(
                role="assistant",
                content=str(content) if content else "",
                tool_calls=tool_calls,
            )
            ctx.append_assistant(msg)
            messages_restored += 1
            if tool_calls:
                pending_tool_ids.extend(tc.id for tc in tool_calls if tc.id)

        elif et == SessionEventType.TOOL_RESULT:
            tool_call_id = str(data.get("tool_call_id", ""))
            content = str(data.get("content", ""))
            is_error = bool(data.get("is_error", False))
            result = ToolResult(
                tool_call_id=tool_call_id,
                content=content,
                is_error=is_error,
            )
            ctx.append_tool_result(result)
            messages_restored += 1
            if tool_call_id in pending_tool_ids:
                pending_tool_ids.remove(tool_call_id)

        elif et == SessionEventType.COMPRESSION:
            # Replace the currently replayed history with the compressed summary.
            # The compression event's data["summary"] is the already-compressed
            # representation; we inject it as a single user message so the LLM
            # sees "here's what happened before" without the raw history.
            summary = str(data.get("summary", ""))
            if summary:
                ctx._history.clear()  # noqa: SLF001 — intentional direct mutation
                pending_tool_ids.clear()  # cleared history can't have dangling tool_use
                ctx.add_user_input(
                    f"[Context compressed — summary of earlier conversation]\n{summary}"
                )
                messages_restored = 1  # reset: only the summary remains
                compressed = True

        elif et == SessionEventType.APPROVAL_DECISION:
            # Snapshot style: the last state wins (it is cumulative).
            state = data.get("state")
            if isinstance(state, dict):
                last_approval_state = state

        # All other event types (TOOL_CALL, CHECKPOINT, UNDO, ERROR,
        # COST_SNAPSHOT) are metadata-only — skip.

    _flush_pending()

    if approval is not None and last_approval_state is not None:
        approval.restore_state(last_approval_state)

    return ReplayStats(
        turns=turns,
        messages_restored=messages_restored,
        compressed=compressed,
    )
