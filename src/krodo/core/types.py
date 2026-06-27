"""Shared data models and enumerations used across all Krodo subsystems.

All types are Pydantic BaseModel or Enum so that:
- LLM tool schemas can be auto-generated from them.
- Validation happens at subsystem boundaries (not inside tools).
- SessionEvent payloads are straightforwardly serialisable to JSON.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Message primitives
# ---------------------------------------------------------------------------


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, object]


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[dict[str, object]]
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] | None = None
    stop_reason: str | None = None
    # M6.2: filled by the provider on assistant responses; ignored elsewhere.
    usage: dict[str, int] | None = None
    cost_usd: float | None = None


class ToolResult(BaseModel):
    tool_call_id: str
    content: str
    is_error: bool = False


# ---------------------------------------------------------------------------
# LLM streaming primitives
# ---------------------------------------------------------------------------


class LLMChunk(BaseModel):
    delta_text: str | None = None
    delta_tool_calls: list[dict[str, object]] | None = None
    usage: dict[str, object] | None = None
    finish_reason: str | None = None


# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------


class ToolDef(BaseModel):
    name: str
    description: str
    parameters: type[BaseModel]


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------

ApprovalMode = Literal["read_only", "auto_edit", "full_auto"]
Decision = Literal["approve", "deny", "approve_session", "approve_pattern"]

# ---------------------------------------------------------------------------
# Session event store (event-sourcing)
# ---------------------------------------------------------------------------


class SessionEventType(StrEnum):
    SESSION_INIT = "session_init"
    USER_MESSAGE = "user_message"
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    APPROVAL_DECISION = "approval_decision"
    CHECKPOINT = "checkpoint"
    UNDO = "undo"
    COMPRESSION = "compression"
    COST_SNAPSHOT = "cost_snapshot"
    ERROR = "error"


class SessionEvent(BaseModel):
    id: str
    session_id: str
    seq: int = Field(..., ge=0, description="Monotonically increasing; guarantees replay order.")
    type: SessionEventType
    timestamp: datetime
    data: dict[str, object]
