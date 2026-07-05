"""Centralised error-recovery logic — architecture.md §7.5 (M3).

Handles 7 scenarios that can interrupt the agent loop:

  1. BAD_JSON       — LLM returned non-parseable tool_call JSON
  2. TOOL_TIMEOUT   — subprocess / tool execution timed out
  3. STALL          — agent issued 3 consecutive identical tool calls
  4. CONTEXT_LOSS   — compression caused loss of critical context
  5. SHA256_CONFLICT — file was externally modified since the LLM last read it
  6. PROVIDER_ERROR — LiteLLM 5xx / rate-limit; exp backoff ×3
  7. EACCES         — file permission denied (not retriable)

Each scenario maps to a ``RecoveryAction`` enum value that tells AgentLoop
what to do next.  The ``handle()`` dispatcher receives the error kind plus a
context dict and returns (action, user_message) so that the loop can inject
a corrective user-visible message into the conversation.

``StallDetector`` tracks tool-call signatures (hash of name+args) across loop
iterations.  When it detects 3 identical consecutive write-tool calls, it
raises ``StallError`` which ``handle()`` maps to STALL.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Recovery action enum
# ---------------------------------------------------------------------------


class RecoveryAction(Enum):
    """What the AgentLoop should do after recovery.handle() returns."""

    RETRY = "retry"  # Re-inject corrective message and retry LLM call
    ABORT = "abort"  # Give up on this turn; surface error to user
    SKIP = "skip"  # Skip the failing tool call, continue loop


# ---------------------------------------------------------------------------
# Error kinds (str constants, not Enum, so callers can pass arbitrary strings)
# ---------------------------------------------------------------------------

BAD_JSON = "bad_json"
TOOL_TIMEOUT = "tool_timeout"
STALL = "stall"
CONTEXT_LOSS = "context_loss"
SHA256_CONFLICT = "sha256_conflict"
PROVIDER_ERROR = "provider_error"
EACCES = "eacces"
INVALID_ARGS = "invalid_args"

# ---------------------------------------------------------------------------
# StallError
# ---------------------------------------------------------------------------


class StallError(RuntimeError):
    """Raised by StallDetector when the agent is stuck in a loop."""

    def __init__(self, tool_name: str, consecutive: int) -> None:
        super().__init__(
            f"Agent stall detected: '{tool_name}' called {consecutive} times consecutively."
        )
        self.tool_name = tool_name
        self.consecutive = consecutive


# ---------------------------------------------------------------------------
# StallDetector
# ---------------------------------------------------------------------------

_STALL_THRESHOLD = 3  # consecutive identical write-tool calls → stall
_WRITE_TOOLS = frozenset(
    {
        "write_file",
        "edit_file",
        "apply_patch",
        "git_commit",
        "run_shell",
    }
)


@dataclass
class StallDetector:
    """Detects when the agent issues identical write-tool calls repeatedly.

    Stall is strictly defined as "``_STALL_THRESHOLD`` consecutive *adjacent*
    identical write-tool calls". Any intervening tool call — read-only OR a
    different write — breaks the consecutive chain and resets the counter.

    Historical note: an earlier version skipped ``record()`` entirely for
    read-only tools, which left ``_consecutive`` unchanged across long
    exploratory sequences. That caused false positives: two identical
    ``run_shell cat foo`` calls separated by 30+ read_file calls were
    incorrectly counted as "3 consecutive" because the reads did not reset
    the counter (see session 5040d7bc.jsonl for the bug repro).
    """

    _last_sig: str | None = field(default=None, init=False)
    _consecutive: int = field(default=0, init=False)
    _recent: list[str] = field(default_factory=list, init=False)

    def record(self, tool_name: str, arguments: dict[str, object]) -> None:
        """Record a tool call.  Raises StallError if stall threshold is exceeded.

        Read-only tools do not count toward the stall counter, but they DO
        break the consecutive chain — any read between two identical writes
        means the writes are no longer "consecutive adjacent".
        """
        sig = _signature(tool_name, arguments)
        self._recent.append(f"{tool_name}({json.dumps(arguments, sort_keys=True)[:80]})")
        if len(self._recent) > _STALL_THRESHOLD:
            self._recent.pop(0)

        is_write = tool_name in _WRITE_TOOLS

        if is_write and sig == self._last_sig:
            # Same write tool called with identical args as the previous
            # *adjacent* write — extend the consecutive chain.
            self._consecutive += 1
        else:
            # Any other call (read-only, or a different write) breaks the
            # chain. If this call is itself a write, it starts a new chain
            # of length 1; reads leave the counter at 0.
            self._consecutive = 1 if is_write else 0
            self._last_sig = sig if is_write else None

        if self._consecutive >= _STALL_THRESHOLD:
            raise StallError(tool_name, self._consecutive)

    def reset(self) -> None:
        """Reset after a successful distinct tool call or turn boundary."""
        self._last_sig = None
        self._consecutive = 0
        self._recent.clear()

    @property
    def recent_calls(self) -> list[str]:
        return list(self._recent)


def _signature(tool_name: str, arguments: dict[str, object]) -> str:
    raw = json.dumps({"name": tool_name, "args": arguments}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# SHA-256 file cache (scenario 5)
# ---------------------------------------------------------------------------

_RECOVERY_SHA256_LIMIT = 50 * 1024 * 1024  # 50 MB — skip hashing above this


def compute_sha256(path: Any) -> str | None:
    """Return hex SHA-256 of *path* contents, or None if file >50 MB or unreadable."""
    try:
        from pathlib import Path

        p = Path(path)
        size = p.stat().st_size
        if size > _RECOVERY_SHA256_LIMIT:
            _logger.warning("sha256 skipped for large file: %s (%d bytes)", path, size)
            return None
        data = p.read_bytes()
        return hashlib.sha256(data).hexdigest()
    except OSError:
        return None


# ---------------------------------------------------------------------------
# handle() dispatcher
# ---------------------------------------------------------------------------


@dataclass
class RecoveryContext:
    """Contextual information passed to handle() for each error kind."""

    error_kind: str
    exception: BaseException | None = None
    tool_name: str | None = None
    tool_args: dict[str, object] | None = None
    retry_count: int = 0
    extra: dict[str, object] = field(default_factory=dict)


async def handle(ctx: RecoveryContext) -> tuple[RecoveryAction, str]:
    """Dispatch error recovery for *ctx.error_kind*.

    Returns ``(action, user_message)`` where *user_message* is a string
    suitable for injection into the conversation so the LLM can self-correct.
    """
    kind = ctx.error_kind

    if kind == BAD_JSON:
        return _handle_bad_json(ctx)
    if kind == TOOL_TIMEOUT:
        return _handle_tool_timeout(ctx)
    if kind == STALL:
        return _handle_stall(ctx)
    if kind == CONTEXT_LOSS:
        return _handle_context_loss(ctx)
    if kind == SHA256_CONFLICT:
        return _handle_sha256_conflict(ctx)
    if kind == PROVIDER_ERROR:
        return await _handle_provider_error(ctx)
    if kind == EACCES:
        return _handle_eacces(ctx)

    _logger.warning("recovery.handle: unknown error kind '%s'", kind)
    return RecoveryAction.ABORT, f"Unrecoverable error ({kind}). Please start a new session."


# ---------------------------------------------------------------------------
# Individual handlers
# ---------------------------------------------------------------------------


def _handle_bad_json(ctx: RecoveryContext) -> tuple[RecoveryAction, str]:
    """Scenario 1: LLM returned non-parseable tool_call JSON."""
    if ctx.retry_count >= 2:
        return (
            RecoveryAction.ABORT,
            "The model failed to produce valid tool-call JSON after 2 retries. "
            "Please try rephrasing your request.",
        )

    schema_hint = str(ctx.extra.get("schema_hint", ""))
    error_detail = str(ctx.exception) if ctx.exception else "unknown JSON parse error"
    msg = (
        f"Your previous tool call could not be parsed: {error_detail}. "
        "Please retry with valid JSON arguments."
    )
    if schema_hint:
        msg += f" Expected schema: {schema_hint}"
    return RecoveryAction.RETRY, msg


def _handle_tool_timeout(ctx: RecoveryContext) -> tuple[RecoveryAction, str]:
    """Scenario 2: tool execution timed out."""
    tool = ctx.tool_name or "unknown tool"
    timeout_s = ctx.extra.get("timeout_seconds", "?")
    return (
        RecoveryAction.SKIP,
        f"Tool '{tool}' timed out after {timeout_s}s. "
        "The partial result has been discarded. "
        "Consider breaking the task into smaller steps.",
    )


def _handle_stall(ctx: RecoveryContext) -> tuple[RecoveryAction, str]:
    """Scenario 3: agent issued 3 consecutive identical tool calls."""
    tool = ctx.tool_name or "unknown tool"
    recent_raw = ctx.extra.get("recent_calls")
    recent: list[object] = recent_raw if isinstance(recent_raw, list) else []
    recent_str = " → ".join(str(c) for c in recent) if recent else "(no details)"
    return (
        RecoveryAction.ABORT,
        f"Agent stall detected: '{tool}' was called identically {_STALL_THRESHOLD} times. "
        f"Recent calls: {recent_str}. "
        "Please check if the operation succeeded already, "
        "or try a different approach.",
    )


def _handle_context_loss(ctx: RecoveryContext) -> tuple[RecoveryAction, str]:
    """Scenario 4: compression may have caused loss of critical context."""
    pinned_raw = ctx.extra.get("pinned_paths")
    pinned: list[object] = pinned_raw if isinstance(pinned_raw, list) else []
    pinned_str = ", ".join(str(p) for p in pinned) if pinned else "(none)"
    return (
        RecoveryAction.RETRY,
        "Context was compressed to stay within the token budget. "
        f"Files that were being worked on: {pinned_str}. "
        "Please re-read any files you need before continuing.",
    )


def _handle_sha256_conflict(ctx: RecoveryContext) -> tuple[RecoveryAction, str]:
    """Scenario 5: file was externally modified since the LLM last read it."""
    path = ctx.extra.get("path", ctx.tool_name or "unknown file")
    return (
        RecoveryAction.SKIP,
        f"File '{path}' was modified externally since it was last read. "
        "The edit was NOT applied. "
        "Please re-read the file to get its current contents before editing.",
    )


async def _handle_provider_error(
    ctx: RecoveryContext,
) -> tuple[RecoveryAction, str]:
    """Scenario 6: LiteLLM 5xx / rate-limit — exponential back-off ×3."""
    if ctx.retry_count >= 3:
        return (
            RecoveryAction.ABORT,
            "The LLM provider returned errors 3 times. "
            "Please check your API key / quota and try again later.",
        )

    backoff = 2**ctx.retry_count  # 1s, 2s, 4s
    _logger.warning(
        "provider_error retry=%d backoff=%ds: %s",
        ctx.retry_count,
        backoff,
        ctx.exception,
    )
    await asyncio.sleep(backoff)
    return (
        RecoveryAction.RETRY,
        f"LLM provider error (retry {ctx.retry_count + 1}/3): {ctx.exception}. Retrying…",
    )


def _handle_eacces(ctx: RecoveryContext) -> tuple[RecoveryAction, str]:
    """Scenario 7: EACCES — file permission denied."""
    path = ctx.extra.get("path", ctx.tool_name or "unknown path")
    perm_bits = ctx.extra.get("permission_bits", "")
    detail = f" (permissions: {perm_bits})" if perm_bits else ""
    return (
        RecoveryAction.SKIP,
        f"Permission denied: cannot write to '{path}'{detail}. "
        "Please check file permissions or ask the user to grant access.",
    )
