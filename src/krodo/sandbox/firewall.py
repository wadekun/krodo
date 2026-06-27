"""LocalSandboxRunner — asyncio-based subprocess executor with path firewall
and shell command blocklist.

Security properties (verified by tests/unit/test_firewall.py):
- is_path_allowed: resolves symlinks before comparing to workspace root.
- is_command_allowed: rejects commands whose first token or full string
  contains any blocklisted pattern.
- run: never uses shell=True; command is passed as a list.
- Subprocess is killed after timeout seconds (SIGKILL to process group).
"""

from __future__ import annotations

import asyncio
import os
import shlex
import signal
from pathlib import Path

from krodo.core.workspace import Workspace

# Commands whose first token is always denied regardless of arguments.
BLOCKLIST_FIRST_TOKEN: frozenset[str] = frozenset({"sudo", "su"})

# Substrings that, if found anywhere in the command string, trigger denial.
BLOCKLIST_SUBSTRINGS: tuple[str, ...] = (
    "rm -rf /",
    "mkfs",
    ":(){:|:&};:",  # fork bomb
    "dd if=/dev/",
    "chmod -R 777 /",
    "chown -R",
)


class LocalSandboxRunner:
    """Runs shell commands in a controlled environment.

    Parameters
    ----------
    workspace:
        The Workspace whose ``root`` is the path-firewall boundary.
    output_limit_bytes:
        Maximum bytes returned from stdout / stderr (excess is truncated).
    """

    def __init__(
        self,
        workspace: Workspace,
        *,
        output_limit_bytes: int = 20_000,
    ) -> None:
        self._workspace = workspace
        self._output_limit = output_limit_bytes

    # ------------------------------------------------------------------
    # SandboxRunner Protocol implementation
    # ------------------------------------------------------------------

    async def run(
        self,
        cmd: list[str],
        cwd: Path,
        timeout: float,
    ) -> tuple[int, str, str]:
        """Execute *cmd* and return (returncode, stdout, stderr).

        *cwd* must be inside the workspace root (checked by caller).
        Never passes shell=True.  Output is truncated to output_limit_bytes.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            return (1, "", f"command not found: {cmd[0]}: {exc}")
        except OSError as exc:
            return (1, "", f"OS error starting process: {exc}")

        try:
            raw_out, raw_err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            await proc.wait()
            return (
                -1,
                "",
                f"[timed out after {timeout}s — process killed]",
            )

        stdout = self._truncate(raw_out.decode("utf-8", errors="replace"))
        stderr = self._truncate(raw_err.decode("utf-8", errors="replace"))
        return (proc.returncode or 0, stdout, stderr)

    def is_path_allowed(self, path: Path) -> bool:
        """Return True iff *path* (after symlink resolution) is under workspace root."""
        try:
            resolved = path.resolve()
        except OSError:
            return False
        return self._workspace.is_path_inside(resolved)

    def is_command_allowed(self, cmd: list[str]) -> bool:
        """Return True iff the command passes all blocklist checks."""
        if not cmd:
            return False
        first_token = shlex.split(cmd[0])[0] if len(cmd) == 1 else cmd[0]
        if first_token in BLOCKLIST_FIRST_TOKEN:
            return False
        full_command = " ".join(cmd)
        for pattern in BLOCKLIST_SUBSTRINGS:
            if pattern in full_command:
                return False
        return True

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _truncate(self, text: str) -> str:
        if len(text.encode()) <= self._output_limit:
            return text
        truncated = text.encode()[: self._output_limit].decode("utf-8", errors="replace")
        return truncated + "\n... [output truncated]"
