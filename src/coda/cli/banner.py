"""Coda CLI banner — printed once per session to confirm workspace identity.

The banner is a Rich Panel that shows:
  - coda version
  - workspace root (absolute path)
  - workspace source (how the root was discovered)
  - approval mode

This implements the §6 invariant:
  "工具分发时 workspace 已注入；banner 在 session 开始时可见"

Usage::

    from coda.cli.banner import print_banner
    print_banner(workspace, approval_mode="auto_edit")
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from coda.core.workspace import Workspace

_console = Console(stderr=False)

try:
    _VERSION = version("coda")
except PackageNotFoundError:
    _VERSION = "dev"


def print_banner(workspace: Workspace, approval_mode: str = "auto_edit") -> None:
    """Print the session banner to stdout using Rich."""
    content = Text()
    content.append("workspace  ", style="bold cyan")
    content.append(str(workspace.root), style="green")
    content.append(f"  [{workspace.source}]\n", style="dim")

    content.append("git        ", style="bold cyan")
    if workspace.git_root is not None:
        content.append(str(workspace.git_root), style="green")
    else:
        content.append("none", style="dim")
    content.append("\n")

    content.append("approval   ", style="bold cyan")
    content.append(approval_mode, style="yellow")

    panel = Panel(
        content,
        title=f"[bold white]coda {_VERSION}[/bold white]",
        border_style="cyan",
        expand=False,
    )
    _console.print(panel)
