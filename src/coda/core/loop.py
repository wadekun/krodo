"""AgentLoop — ReAct loop with M3 recovery and stall detection.

Drives the conversation: sends user input to the LLM, dispatches tool calls,
feeds results back, and terminates when the model returns a final text answer
or the tool-call budget is exhausted.

M3 additions:
- StallDetector: raises StallError when the same write-tool is called ≥3 times.
- recovery.handle(): maps errors to (action, user_message) for self-correction.
- await context_manager.compress_if_needed(): defers to the Compressor.

Architecture: see docs/architecture.md §3.3 and §4.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from coda.core.context import InMemoryContextManager
from coda.core.recovery import (
    BAD_JSON,
    EACCES,
    PROVIDER_ERROR,
    STALL,
    TOOL_TIMEOUT,
    RecoveryAction,
    RecoveryContext,
    StallDetector,
    StallError,
    handle,
)
from coda.core.types import (
    Message,
    ToolCall,
    ToolResult,
)
from coda.llm.protocols import LLMProvider
from coda.sandbox.protocols import ApprovalManager
from coda.tools.protocols import ToolContext
from coda.tools.registry import ToolRegistry

_DEFAULT_MAX_TOOL_CALLS = 15
_logger = logging.getLogger(__name__)


@dataclass
class LoopConfig:
    max_tool_calls_per_turn: int = _DEFAULT_MAX_TOOL_CALLS
    system_prompt: str = (
        "You are Coda, a coding assistant. "
        "Use the provided tools to complete the user's request. "
        "When done, reply with a concise summary of what you did."
    )


@dataclass
class TurnResult:
    """Outcome of a single agent turn."""

    final_text: str
    tool_calls_made: int = 0
    aborted_by_user: bool = False
    hit_tool_call_limit: bool = False


class AgentLoop:
    """Single-turn ReAct loop with error recovery and stall detection.

    One `run()` call handles one user request end-to-end.
    Multi-turn state (history) is kept in `context_manager` and persists
    across `run()` calls on the same instance.
    """

    def __init__(
        self,
        provider: LLMProvider,
        registry: ToolRegistry,
        tool_ctx: ToolContext,
        approval: ApprovalManager,
        *,
        config: LoopConfig | None = None,
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._tool_ctx = tool_ctx
        self._approval = approval
        self._config = config or LoopConfig()
        self._logger = tool_ctx.logger
        self._stall = StallDetector()
        self.context_manager = InMemoryContextManager(
            system_prompt=self._config.system_prompt,
        )

    async def run(self, user_input: str) -> TurnResult:  # noqa: C901
        """Execute one full agent turn for *user_input*."""
        self.context_manager.add_user_input(user_input)
        self._stall.reset()

        messages = self.context_manager.build_messages()
        tool_defs = self._registry.all_defs()

        tool_calls_made = 0
        final_text = ""
        hit_limit = False
        aborted = False

        # Track retry count for provider errors (per-turn)
        provider_retry = 0

        while True:
            # ----------------------------------------------------------------
            # LLM call with provider-error recovery
            # ----------------------------------------------------------------
            try:
                response: Message = await self._provider.chat(
                    messages=messages,
                    tools=tool_defs,
                )
                provider_retry = 0  # reset on success
            except Exception as exc:  # noqa: BLE001
                action, msg = await handle(
                    RecoveryContext(
                        error_kind=PROVIDER_ERROR,
                        exception=exc,
                        retry_count=provider_retry,
                    )
                )
                provider_retry += 1
                if action == RecoveryAction.RETRY:
                    self.context_manager.add_user_input(msg)
                    messages = self.context_manager.build_messages()
                    continue
                # ABORT
                final_text = msg
                break

            # ----------------------------------------------------------------
            # Validate tool_call JSON
            # ----------------------------------------------------------------
            if response.tool_calls:
                bad_json_retry = 0
                for tc in response.tool_calls:
                    if "_raw" in (tc.arguments or {}):
                        raw_str = tc.arguments.get("_raw", "")
                        action, recovery_msg = await handle(
                            RecoveryContext(
                                error_kind=BAD_JSON,
                                exception=json.JSONDecodeError(
                                    "invalid JSON from LLM", str(raw_str), 0
                                ),
                                retry_count=bad_json_retry,
                                extra={"schema_hint": tc.name},
                            )
                        )
                        bad_json_retry += 1
                        if action == RecoveryAction.RETRY:
                            self.context_manager.add_user_input(recovery_msg)
                            messages = self.context_manager.build_messages()
                            break
                        # ABORT
                        final_text = recovery_msg
                        aborted = True
                        break
                if aborted:
                    break

            if not response.tool_calls:
                # Model produced a final answer — store and return
                final_text = response.content if isinstance(response.content, str) else ""
                self.context_manager.append_assistant(response)
                break

            # ----------------------------------------------------------------
            # Process each tool call
            # ----------------------------------------------------------------
            self.context_manager.append_assistant(response)

            for tc in response.tool_calls:
                if tool_calls_made >= self._config.max_tool_calls_per_turn:
                    hit_limit = True
                    self._logger.warning(
                        "max_tool_calls_reached limit=%d",
                        self._config.max_tool_calls_per_turn,
                    )
                    break

                # Stall detection
                try:
                    self._stall.record(tc.name, tc.arguments or {})
                except StallError as exc:
                    action, msg = await handle(
                        RecoveryContext(
                            error_kind=STALL,
                            exception=exc,
                            tool_name=tc.name,
                            extra={"recent_calls": self._stall.recent_calls},
                        )
                    )
                    final_text = msg
                    aborted = True
                    break

                decision = await self._approval.check(
                    ToolCall(
                        id=tc.id or "",
                        name=tc.name,
                        arguments=tc.arguments or {},
                    )
                )

                if decision == "deny":
                    aborted = True
                    result = ToolResult(
                        tool_call_id=tc.id or "",
                        content="[user denied tool call]",
                        is_error=True,
                    )
                else:
                    try:
                        result = await self._registry.execute(
                            tc.name,
                            tc.arguments or {},
                            self._tool_ctx,
                        )
                    except TimeoutError as exc:
                        action, msg = await handle(
                            RecoveryContext(
                                error_kind=TOOL_TIMEOUT,
                                exception=exc,
                                tool_name=tc.name,
                                extra={"timeout_seconds": 30},
                            )
                        )
                        result = ToolResult(
                            tool_call_id=tc.id or "",
                            content=msg,
                            is_error=True,
                        )
                    except PermissionError as exc:
                        path = tc.arguments.get("path", "") if tc.arguments else ""
                        _, msg = await handle(
                            RecoveryContext(
                                error_kind=EACCES,
                                exception=exc,
                                tool_name=tc.name,
                                extra={"path": path},
                            )
                        )
                        result = ToolResult(
                            tool_call_id=tc.id or "",
                            content=msg,
                            is_error=True,
                        )

                    result = ToolResult(
                        tool_call_id=tc.id or "",
                        content=result.content,
                        is_error=result.is_error,
                    )

                self.context_manager.append_tool_result(result)
                tool_calls_made += 1

                if aborted:
                    break

            if hit_limit or aborted:
                break

            # Compress context and rebuild messages for next LLM call
            await self.context_manager.compress_if_needed()
            messages = self.context_manager.build_messages()

        return TurnResult(
            final_text=final_text,
            tool_calls_made=tool_calls_made,
            aborted_by_user=aborted,
            hit_tool_call_limit=hit_limit,
        )
