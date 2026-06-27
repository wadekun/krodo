"""krodo undo — restore files to the previous git checkpoint (M4 PR4).

Reads the JSONL session event log (``<workspace>/.krodo/sessions/<session_id>.jsonl``)
to find the most recent ``CHECKPOINT`` event, then restores only the paths listed
in that checkpoint's ``affected_paths`` via ``git checkout <sha> -- <paths>``.

Emits an ``UNDO`` SessionEvent on success.

Usage::

    # Undo within a specific session
    krodo undo --session <session_id> [--root <workspace>]

    # Auto-detect: find the most recent session JSONL in the workspace
    krodo undo [--root <workspace>]

Non-git workspaces: exits with a clear error message and exit code 1.
No CHECKPOINT found: exits with a clear error message and exit code 1.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer

from krodo.core.workspace import LocalWorkspaceResolver
from krodo.sandbox.checkpoint import CheckpointError, GitCheckpointManager

_SESSIONS_DIR_NAME = ".krodo/sessions"


def undo_command(
    root: Path | None = None,
    session: str | None = None,
    *,
    _workspace_root: Path | None = None,  # injected in tests
) -> None:
    """Restore files to the previous checkpoint in the active session.

    Parameters
    ----------
    root:
        Workspace root path.  Defaults to auto-discovery.
    session:
        Session ID (JSONL filename stem).  Defaults to the most recently
        modified JSONL file found in ``<workspace>/.krodo/sessions/``.
    _workspace_root:
        Test-only injection of a resolved workspace root (skips resolver).
    """
    from rich.console import Console  # noqa: PLC0415

    console = Console()

    # Resolve workspace
    if _workspace_root is not None:
        workspace_root = _workspace_root
    else:
        resolver = LocalWorkspaceResolver()
        workspace = resolver.resolve(explicit=root)
        workspace_root = workspace.root

    # Find the JSONL file in the sessions directory
    sessions_dir = workspace_root / _SESSIONS_DIR_NAME
    jsonl_path = _resolve_jsonl(sessions_dir, session)
    if jsonl_path is None:
        if session:
            console.print(
                f"[red]No session file found for session '{session}' in {sessions_dir}[/red]"
            )
        else:
            console.print(
                f"[red]No session files found in {sessions_dir}. "
                "Did you run krodo in this workspace?[/red]"
            )
        raise typer.Exit(code=1)

    # Find most recent CHECKPOINT event
    checkpoint = _find_latest_checkpoint(jsonl_path)
    if checkpoint is None:
        console.print(
            f"[red]No checkpoint found in session {jsonl_path.stem}. "
            "Did the previous run write anything?[/red]"
        )
        console.print(f"[dim]Log file: {jsonl_path}[/dim]")
        raise typer.Exit(code=1)

    sha: str = str(checkpoint.get("sha", ""))
    raw_paths: object = checkpoint.get("affected_paths", [])
    affected_paths: list[Path] = []
    if isinstance(raw_paths, list):
        affected_paths = [Path(str(p)) for p in raw_paths]

    if not sha or not affected_paths:
        console.print("[red]Checkpoint event is missing sha or affected_paths.[/red]")
        raise typer.Exit(code=1)

    # Detect non-git workspace
    resolver2 = LocalWorkspaceResolver()
    ws = resolver2.resolve(explicit=workspace_root)
    mgr = GitCheckpointManager(ws)
    if mgr.git_root is None:
        console.print(
            "[red]This session was not run inside a git repository. krodo undo requires git.[/red]"
        )
        raise typer.Exit(code=1)

    # Warn if affected_paths covers the whole workspace root (run_shell scope)
    if len(affected_paths) == 1 and affected_paths[0] == workspace_root:
        console.print(
            "[yellow]Warning: this checkpoint covers the entire workspace root "
            "(created by a shell command). Restoring may roll back more than expected.[/yellow]"
        )
        confirmed = typer.confirm("Continue anyway?", default=False)
        if not confirmed:
            raise typer.Exit(code=0)

    # Restore
    try:
        mgr.restore(sha, affected_paths)
    except CheckpointError as exc:
        console.print(f"[red]Restore failed: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    # Emit UNDO event
    session_id = jsonl_path.stem
    _emit_undo_event(
        jsonl_path=jsonl_path,
        session_id=session_id,
        sha=sha,
        affected_paths=[str(p) for p in affected_paths],
    )

    console.print(f"[green]Restored {len(affected_paths)} path(s) to checkpoint {sha[:8]}…[/green]")
    for p in affected_paths:
        console.print(f"  [dim]{p}[/dim]")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_jsonl(logs_dir: Path, session: str | None) -> Path | None:
    """Find the JSONL file for *session* or the most recently modified one."""
    if not logs_dir.exists():
        return None
    if session:
        candidate = logs_dir / f"{session}.jsonl"
        return candidate if candidate.exists() else None
    # Auto-detect: most recent file
    jsonl_files = list(logs_dir.glob("*.jsonl"))
    if not jsonl_files:
        return None
    return max(jsonl_files, key=lambda p: p.stat().st_mtime)


def _find_latest_checkpoint(jsonl_path: Path) -> dict[str, Any] | None:
    """Return the ``data`` dict from the most recent CHECKPOINT event."""
    latest: dict[str, Any] | None = None
    latest_seq = -1

    try:
        lines = jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "checkpoint":
            continue
        seq = int(event.get("seq", -1))
        if seq > latest_seq:
            latest_seq = seq
            data = event.get("data", {})
            if isinstance(data, dict):
                latest = data

    return latest


def _emit_undo_event(
    jsonl_path: Path,
    session_id: str,
    sha: str,
    affected_paths: list[str],
) -> None:
    """Append an UNDO SessionEvent to the session JSONL."""
    import uuid  # noqa: PLC0415

    from krodo.core.events import SessionEventLogger  # noqa: PLC0415
    from krodo.core.types import SessionEventType  # noqa: PLC0415

    event_logger = SessionEventLogger(
        session_id=session_id,
        jsonl_path=jsonl_path,
    )
    event_logger.emit(
        SessionEventType.UNDO,
        data={
            "sha": sha,
            "affected_paths": affected_paths,
        },
        event_id=str(uuid.uuid4()),
    )


def register_undo_app(app: typer.Typer) -> None:
    """Register the ``krodo undo`` subcommand using add_typer so the main
    command remains invokable without a subcommand prefix."""
    undo_sub = typer.Typer(
        name="undo",
        help="Restore files to the previous checkpoint created by Krodo.",
        add_completion=False,
        invoke_without_command=True,
    )

    @undo_sub.callback(invoke_without_command=True)
    def _undo(
        root: Path | None = typer.Option(
            None,
            "--root",
            "-r",
            help="Workspace root (default: auto-discover from cwd)",
            envvar="KRODO_ROOT",
        ),
        session: str | None = typer.Option(
            None,
            "--session",
            "-s",
            help="Session ID to undo (default: most recent in workspace)",
        ),
    ) -> None:
        """Restore files to the previous checkpoint created by Krodo."""
        undo_command(root=root, session=session)

    app.add_typer(undo_sub)
