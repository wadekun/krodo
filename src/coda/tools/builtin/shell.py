"""Built-in shell tool: run_shell.

Security: the command is parsed with shlex and passed to SandboxRunner.run()
as a list — never shell=True.  Blocklist and path-firewall checks happen inside
SandboxRunner, so approval must be obtained before calling execute().

M4: Run-before-write heuristic — if the command looks like it will modify the
filesystem, a git stash checkpoint is created before execution.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from pydantic import BaseModel, Field

from coda.core.types import SessionEventType, ToolDef, ToolResult
from coda.sandbox.checkpoint import shell_command_writes
from coda.tools.protocols import ToolContext

_DEFAULT_TIMEOUT = 60.0
_DEFAULT_CWD_SENTINEL = "."


class RunShellParams(BaseModel):
    command: str = Field(description="Shell command string; parsed with shlex (no shell=True).")
    cwd: str = Field(
        default=_DEFAULT_CWD_SENTINEL,
        description="Working directory relative to workspace root. Defaults to workspace root.",
    )
    timeout: float = Field(default=_DEFAULT_TIMEOUT, ge=0.1, le=300)


class RunShellTool:
    definition = ToolDef(
        name="run_shell",
        description=(
            "Run a shell command inside the project workspace. "
            "The command is parsed with shlex (no shell=True). "
            "Output (stdout + stderr) is returned as text. "
            "Requires user approval before execution."
        ),
        parameters=RunShellParams,
    )
    requires_approval = True

    async def execute(self, args: dict[str, object], ctx: ToolContext) -> ToolResult:
        params = RunShellParams.model_validate(args)

        # Parse command string into argv
        try:
            argv = shlex.split(params.command)
        except ValueError as exc:
            return ToolResult(
                tool_call_id="",
                content=f"ERROR: cannot parse command: {exc}",
                is_error=True,
            )
        if not argv:
            return ToolResult(tool_call_id="", content="ERROR: empty command", is_error=True)

        # Blocklist check
        if not ctx.sandbox.is_command_allowed(argv):
            return ToolResult(
                tool_call_id="",
                content=f"DENIED (policy): command blocked: {params.command!r}",
                is_error=True,
            )

        # Resolve cwd
        cwd = self._resolve_cwd(params.cwd, ctx)
        if isinstance(cwd, str):  # error
            return ToolResult(tool_call_id="", content=cwd, is_error=True)

        # Checkpoint before commands that look like they write to disk (§5.4)
        if shell_command_writes(params.command):
            # Fallback scope: entire workspace root (conservative but safe)
            sha = await ctx.checkpoint.create([ctx.workspace.root])
            if sha and ctx.event_logger is not None:
                from coda.core.events import SessionEventLogger  # noqa: PLC0415

                if isinstance(ctx.event_logger, SessionEventLogger):
                    ctx.event_logger.emit(
                        SessionEventType.CHECKPOINT,
                        data={
                            "sha": sha,
                            "affected_paths": [str(ctx.workspace.root)],
                            "tool": "run_shell",
                            "scope": "approximate",
                            "command": params.command,
                        },
                    )

        # Execute via sandbox
        returncode, stdout, stderr = await ctx.sandbox.run(argv, cwd=cwd, timeout=params.timeout)

        parts: list[str] = []
        if stdout:
            parts.append(stdout)
        if stderr:
            parts.append(f"[stderr]\n{stderr}")
        output = "\n".join(parts) if parts else ""

        is_error = returncode != 0
        prefix = f"[exit {returncode}]\n" if returncode != 0 else ""
        return ToolResult(tool_call_id="", content=f"{prefix}{output}", is_error=is_error)

    @staticmethod
    def _resolve_cwd(cwd_str: str, ctx: ToolContext) -> Path | str:
        if cwd_str == _DEFAULT_CWD_SENTINEL or not cwd_str:
            return ctx.workspace.root
        try:
            candidate = (ctx.workspace.root / cwd_str).resolve()
        except (OSError, RuntimeError) as exc:
            return f"ERROR: cannot resolve cwd '{cwd_str}': {exc}"
        if not ctx.workspace.is_path_inside(candidate):
            return f"ERROR: cwd '{cwd_str}' is outside workspace root"
        if not candidate.is_dir():
            return f"ERROR: cwd '{cwd_str}' is not a directory"
        return candidate
