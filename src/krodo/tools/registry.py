"""ToolRegistry — registration, lookup, and OpenAI JSON-schema generation for tools.

Usage::

    registry = ToolRegistry()
    registry.register(ReadFileTool())

    # Get the list of tool definitions for LiteLLM
    schemas: list[ToolDef] = registry.all_defs()

    # Dispatch by name
    result = await registry.execute("read_file", args, ctx)
"""

from __future__ import annotations

from krodo.core.types import ToolDef, ToolResult
from krodo.tools.protocols import Tool, ToolContext


class ToolRegistry:
    """Central registry for all tools available to the agent."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register *tool*.  Raises ValueError if the name is already taken."""
        name = tool.definition.name
        if name in self._tools:
            raise ValueError(f"Tool '{name}' is already registered")
        self._tools[name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all_defs(self) -> list[ToolDef]:
        """Return all ToolDef objects (used to build the 'tools' param for LiteLLM)."""
        return [t.definition for t in self._tools.values()]

    async def execute(self, name: str, args: dict[str, object], ctx: ToolContext) -> ToolResult:
        """Dispatch a tool call by name.  Returns an error ToolResult if not found."""
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(
                tool_call_id="",
                content=f"ERROR: unknown tool '{name}'",
                is_error=True,
            )
        try:
            return await tool.execute(args, ctx)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                tool_call_id="",
                content=f"ERROR: tool '{name}' raised {type(exc).__name__}: {exc}",
                is_error=True,
            )

    def __len__(self) -> int:
        return len(self._tools)

    def names(self) -> list[str]:
        return list(self._tools.keys())
