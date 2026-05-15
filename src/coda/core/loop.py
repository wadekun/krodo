"""AgentLoop — minimal ReAct loop implementation (M1).

Drives the conversation: sends user input to the LLM, dispatches tool calls,
feeds results back, and terminates when the model returns a final text answer
or the tool-call budget is exhausted.

Architecture: see docs/architecture.md §3.3 and §4.
"""

from __future__ import annotations

from dataclasses import dataclass

from coda.core.context import InMemoryContextManager
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
    """Minimal single-turn ReAct loop.

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
        self.context_manager = InMemoryContextManager(
            system_prompt=self._config.system_prompt,
        )

    async def run(self, user_input: str) -> TurnResult:
        """Execute one full agent turn for *user_input*."""
        self.context_manager.add_user_input(user_input)
        messages = self.context_manager.build_messages()
        tool_defs = self._registry.all_defs()

        tool_calls_made = 0
        final_text = ""
        hit_limit = False
        aborted = False

        while True:
            response: Message = await self._provider.chat(
                messages=messages,
                tools=tool_defs,
            )

            if not response.tool_calls:
                # Model produced a final answer — store and return
                final_text = response.content if isinstance(response.content, str) else ""
                self.context_manager.append_assistant(response)
                break

            # Process each tool call in the response
            self.context_manager.append_assistant(response)

            for tc in response.tool_calls:
                if tool_calls_made >= self._config.max_tool_calls_per_turn:
                    hit_limit = True
                    self._logger.warning(
                        "max_tool_calls_reached limit=%d",
                        self._config.max_tool_calls_per_turn,
                    )
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
                    result = await self._registry.execute(
                        tc.name,
                        tc.arguments or {},
                        self._tool_ctx,
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

            # Rebuild messages with the tool results appended
            await self.context_manager.compress_if_needed()
            messages = self.context_manager.build_messages()

        return TurnResult(
            final_text=final_text,
            tool_calls_made=tool_calls_made,
            aborted_by_user=aborted,
            hit_tool_call_limit=hit_limit,
        )
