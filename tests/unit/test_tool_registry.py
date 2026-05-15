"""Tests for ToolRegistry."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from pydantic import BaseModel

from coda.core.types import ToolDef, ToolResult
from coda.core.workspace import LocalWorkspaceResolver
from coda.sandbox.firewall import LocalSandboxRunner
from coda.tools.protocols import Tool, ToolContext
from coda.tools.registry import ToolRegistry


class EchoParams(BaseModel):
    message: str


class EchoTool:
    definition = ToolDef(name="echo", description="Echo the message", parameters=EchoParams)
    requires_approval = False

    async def execute(self, args: dict[str, object], ctx: ToolContext) -> ToolResult:
        params = EchoParams.model_validate(args)
        return ToolResult(tool_call_id="", content=params.message)


class BrokenTool:
    definition = ToolDef(name="broken", description="Always raises", parameters=EchoParams)
    requires_approval = False

    async def execute(self, args: dict[str, object], ctx: ToolContext) -> ToolResult:
        raise RuntimeError("intentional failure")


def _ctx(tmp_path: Path) -> ToolContext:
    ws = LocalWorkspaceResolver().resolve(explicit=tmp_path)
    sb = LocalSandboxRunner(ws)
    return ToolContext(
        workspace=ws, sandbox=sb, session_id="test", logger=logging.getLogger("test")
    )


def test_register_and_len() -> None:
    reg = ToolRegistry()
    reg.register(EchoTool())
    assert len(reg) == 1
    assert "echo" in reg.names()


def test_register_duplicate_raises() -> None:
    reg = ToolRegistry()
    reg.register(EchoTool())
    with pytest.raises(ValueError, match="already registered"):
        reg.register(EchoTool())


def test_all_defs_returns_tool_defs() -> None:
    reg = ToolRegistry()
    reg.register(EchoTool())
    defs = reg.all_defs()
    assert len(defs) == 1
    assert defs[0].name == "echo"


def test_get_returns_none_for_unknown() -> None:
    reg = ToolRegistry()
    assert reg.get("nope") is None


@pytest.mark.asyncio
async def test_execute_known_tool(tmp_path: Path) -> None:
    reg = ToolRegistry()
    reg.register(EchoTool())
    result = await reg.execute("echo", {"message": "hi"}, _ctx(tmp_path))
    assert result.content == "hi"
    assert not result.is_error


@pytest.mark.asyncio
async def test_execute_unknown_tool_returns_error(tmp_path: Path) -> None:
    reg = ToolRegistry()
    result = await reg.execute("unknown", {}, _ctx(tmp_path))
    assert result.is_error
    assert "unknown tool" in result.content


@pytest.mark.asyncio
async def test_execute_tool_exception_returns_error(tmp_path: Path) -> None:
    reg = ToolRegistry()
    reg.register(BrokenTool())
    result = await reg.execute("broken", {"message": "x"}, _ctx(tmp_path))
    assert result.is_error
    assert "RuntimeError" in result.content


def test_protocol_compliance() -> None:
    """EchoTool satisfies the Tool Protocol at runtime."""
    assert isinstance(EchoTool(), Tool)
