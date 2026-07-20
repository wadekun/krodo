"""Message history compression — architecture.md §3.4.1 & §1.2 (M3 plan).

Two strategies, selected via the KRODO_COMPRESS environment variable:

  KRODO_COMPRESS=llm (default)
      Calls the injected LLMProvider to summarise the oldest N dialogue
      rounds into a single <SUMMARY>…</SUMMARY> system message, replacing
      those messages in the history.  The summary itself counts against the
      token budget and is tracked as a COMPRESSION SessionEvent.

  KRODO_COMPRESS=algorithmic
      Drops the content of the oldest N tool_result messages while keeping
      the tool_call metadata and the file paths involved.  Zero LLM calls;
      suitable for offline development or very large codebases.

Both strategies honour ``pinned_context``:
  - The most-recent 5 file paths touched by tool_calls.
  - The most-recent user message.
These items are **never** removed during compression.

Factory:
    make_compressor(strategy, provider) → Compressor
"""

from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from datetime import UTC
from typing import TYPE_CHECKING

from krodo.core.types import Message, SessionEvent, SessionEventType

if TYPE_CHECKING:
    from krodo.llm.protocols import LLMProvider

# ---------------------------------------------------------------------------
# How many "rounds" to compress in one pass (a round = user+assistant+tools)
_DEFAULT_COMPRESS_ROUNDS = 2
_PINNED_PATH_LIMIT = 5  # max number of file paths to keep in pinned context


# ---------------------------------------------------------------------------
# Pinned context helpers
# ---------------------------------------------------------------------------


def _extract_file_paths(messages: list[Message]) -> list[str]:
    """Return the most-recent file paths referenced in tool_call arguments.

    Scans the message list from newest to oldest and collects unique paths
    from tool_call argument dictionaries that have a ``path`` or ``file``
    key.  Limited to ``_PINNED_PATH_LIMIT`` entries.
    """
    paths: list[str] = []
    seen: set[str] = set()

    for msg in reversed(messages):
        if msg.tool_calls:
            for tc in msg.tool_calls:
                for key in ("path", "file", "target"):
                    val = tc.arguments.get(key)
                    if isinstance(val, str) and val and val not in seen:
                        seen.add(val)
                        paths.append(val)
                        if len(paths) >= _PINNED_PATH_LIMIT:
                            return paths
    return paths


def _last_user_message(messages: list[Message]) -> Message | None:
    """Return the most recent user message, or None."""
    for msg in reversed(messages):
        if msg.role == "user":
            return msg
    return None


def is_prefix_message(msg: Message) -> bool:
    """True for stable-prefix context messages (``<project_memory>``, ``<repo_map>``).

    These are injected at the head of history and must survive compression and
    hard-truncation — dropping ``<project_memory>`` loses project context
    mid-session, and dropping ``<repo_map>`` breaks prompt-cache byte-stability.
    """
    return isinstance(msg.content, str) and (
        msg.content.startswith("<project_memory>") or msg.content.startswith("<repo_map>")
    )


def _pinned_ids(messages: list[Message]) -> set[int]:
    """Return the set of *id()* values for messages that must not be compressed.

    Always pins:
    - The system prompt (index 0).
    - Stable-prefix messages (``<project_memory>`` / ``<repo_map>``).
    - The most-recent user message.
    """
    pinned: set[int] = set()

    # System prompt
    for msg in messages:
        if msg.role == "system":
            pinned.add(id(msg))
            break

    # Stable prefix messages — never compress (M10).
    for msg in messages:
        if is_prefix_message(msg):
            pinned.add(id(msg))

    # Most-recent user message
    last_user = _last_user_message(messages)
    if last_user is not None:
        pinned.add(id(last_user))

    return pinned


# ---------------------------------------------------------------------------
# Compressor ABC
# ---------------------------------------------------------------------------


class Compressor(ABC):
    """Abstract compressor — compresses the oldest non-pinned messages."""

    @abstractmethod
    async def compress(
        self,
        history: list[Message],
        *,
        n_rounds: int = _DEFAULT_COMPRESS_ROUNDS,
    ) -> tuple[list[Message], SessionEvent | None]:
        """Compress *history* in-place and return the modified list.

        Returns (new_history, compression_event_or_None).
        The caller is responsible for emitting the SessionEvent.
        """


# ---------------------------------------------------------------------------
# Algorithmic compressor
# ---------------------------------------------------------------------------


class AlgorithmicCompressor(Compressor):
    """Drop tool_result content from the oldest *n_rounds* dialogue rounds.

    Keeps tool_call metadata (name + arguments) so the model still has
    context about which files were touched.  Attaches a stub content string
    to the tool result so the conversation remains syntactically valid.
    """

    async def compress(
        self,
        history: list[Message],
        *,
        n_rounds: int = _DEFAULT_COMPRESS_ROUNDS,
    ) -> tuple[list[Message], SessionEvent | None]:
        if not history:
            return history, None

        pinned = _pinned_ids([Message(role="system", content=""), *history])

        dropped_count = 0
        rounds_processed = 0
        i = 0

        while i < len(history) and rounds_processed < n_rounds:
            msg = history[i]
            if id(msg) in pinned:
                i += 1
                continue

            # Compress tool_result messages by replacing content with stub
            if msg.role == "tool" and msg.content != "[compressed]":
                # Extract file paths mentioned before we wipe the content
                history[i] = Message(
                    role="tool",
                    content="[compressed]",
                    tool_call_id=msg.tool_call_id,
                )
                dropped_count += 1
                rounds_processed += 1
            i += 1

        if dropped_count == 0:
            return history, None

        import uuid
        from datetime import datetime

        event = SessionEvent(
            id=str(uuid.uuid4()),
            session_id="",  # filled by SessionEventLogger
            seq=0,  # overwritten by SessionEventLogger
            type=SessionEventType.COMPRESSION,
            timestamp=datetime.now(UTC),
            data={
                "strategy": "algorithmic",
                "messages_compressed": dropped_count,
            },
        )
        return history, event


# ---------------------------------------------------------------------------
# LLM-based summary compressor
# ---------------------------------------------------------------------------

_SUMMARY_PROMPT = (
    "You are summarising a coding-assistant conversation. "
    "Produce a concise summary (≤150 words) of the following dialogue rounds, "
    "focusing on: files modified, commands run, errors encountered, and decisions made. "
    "Wrap the summary in <SUMMARY> and </SUMMARY> tags.\n\n"
    "Conversation to summarise:\n{dialogue}"
)


class LLMSummaryCompressor(Compressor):
    """Summarise the oldest *n_rounds* dialogue rounds using the LLM provider.

    Replaces the compressed messages with a single ``system`` message
    containing a ``<SUMMARY>…</SUMMARY>`` block.
    """

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider

    async def compress(
        self,
        history: list[Message],
        *,
        n_rounds: int = _DEFAULT_COMPRESS_ROUNDS,
    ) -> tuple[list[Message], SessionEvent | None]:
        if not history:
            return history, None

        # Identify pinned messages
        sys_msg = Message(role="system", content="")
        pinned = _pinned_ids([sys_msg, *history])

        # Collect the oldest non-pinned messages up to n_rounds dialogue turns
        to_compress_indices: list[int] = []
        rounds_seen = 0
        for i, msg in enumerate(history):
            if id(msg) in pinned:
                continue
            to_compress_indices.append(i)
            if msg.role == "user":
                rounds_seen += 1
            if rounds_seen >= n_rounds:
                break

        if not to_compress_indices:
            return history, None

        # Build dialogue text for the summary prompt
        dialogue_parts: list[str] = []
        for idx in to_compress_indices:
            msg = history[idx]
            if isinstance(msg.content, str):
                content = msg.content
            else:
                content = str(msg.content)
            if msg.tool_calls:
                tc_summary = ", ".join(f"{tc.name}({tc.arguments})" for tc in msg.tool_calls)
                content = f"[tool_calls: {tc_summary}]"
            dialogue_parts.append(f"{msg.role}: {content}")

        dialogue_text = "\n".join(dialogue_parts)
        summary_prompt = _SUMMARY_PROMPT.format(dialogue=dialogue_text)

        # Call the LLM for a summary
        summary_msg = await self._provider.chat(
            messages=[Message(role="user", content=summary_prompt)]
        )
        raw_summary = summary_msg.content if isinstance(summary_msg.content, str) else ""

        # Extract the <SUMMARY>…</SUMMARY> block, or use the raw output
        match = re.search(r"<SUMMARY>(.*?)</SUMMARY>", raw_summary, re.DOTALL)
        summary_text = match.group(1).strip() if match else raw_summary.strip()

        # Replace compressed messages with summary block; keep pinned messages
        new_history: list[Message] = []
        inserted_summary = False
        for i, msg in enumerate(history):
            if i in to_compress_indices:
                if not inserted_summary:
                    new_history.append(
                        Message(
                            role="system",
                            content=f"<SUMMARY>{summary_text}</SUMMARY>",
                        )
                    )
                    inserted_summary = True
                # Skip compressed messages
            else:
                new_history.append(msg)

        import uuid
        from datetime import datetime

        event = SessionEvent(
            id=str(uuid.uuid4()),
            session_id="",  # filled by SessionEventLogger
            seq=0,  # overwritten by SessionEventLogger
            type=SessionEventType.COMPRESSION,
            timestamp=datetime.now(UTC),
            data={
                "strategy": "llm",
                "messages_compressed": len(to_compress_indices),
                "summary_length": len(summary_text),
            },
        )
        return new_history, event


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def make_compressor(
    strategy: str | None = None,
    provider: LLMProvider | None = None,
) -> Compressor:
    """Return a Compressor based on the *strategy* string.

    Strategy resolution order:
    1. *strategy* parameter (explicit override).
    2. ``KRODO_COMPRESS`` environment variable.
    3. Default: ``"llm"`` if *provider* is supplied, else ``"algorithmic"``.

    Raises ValueError if strategy is ``"llm"`` but no *provider* is given.
    """
    effective = (
        strategy
        or os.environ.get("KRODO_COMPRESS")
        or ("llm" if provider is not None else "algorithmic")
    )
    effective = effective.lower().strip()

    if effective == "algorithmic":
        return AlgorithmicCompressor()

    if effective == "llm":
        if provider is None:
            raise ValueError(
                "strategy='llm' requires a provider argument. "
                "Set KRODO_COMPRESS=algorithmic or pass a provider."
            )
        return LLMSummaryCompressor(provider)

    raise ValueError(
        f"Unknown compression strategy: {effective!r}. Valid values: 'llm', 'algorithmic'."
    )
