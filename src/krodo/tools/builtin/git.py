"""Built-in git tools: git_status, git_diff, git_commit.

All three tools require ``ctx.workspace.git_root`` to be set.  If the
workspace is not inside a git repository the tools return a clear error
instead of trying (and failing) to create a Repo object.

Implementation uses GitPython (``git>=3.1``) rather than raw subprocess so
that path handling is cross-platform and typed.

Security note: git_commit redacts API key literals (sk-*, OPENAI_API_KEY=,
etc.) from the commit message before passing it to git; the raw credentials
can never end up in the commit log.
"""

from __future__ import annotations

import re

import git as gitpython
from pydantic import BaseModel, Field

from krodo.core.types import ToolDef, ToolResult
from krodo.tools.protocols import ToolContext

# Pattern to redact API key literals from commit messages
_API_KEY_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{10,}|"
    r"(?:OPENAI|ANTHROPIC|DEEPSEEK|ZHIPU|OPENROUTER)_API_KEY\s*=\s*\S+)",
    re.IGNORECASE,
)
_REDACTED = "[REDACTED]"


def _redact_secrets(text: str) -> str:
    return _API_KEY_RE.sub(_REDACTED, text)


def _get_repo(ctx: ToolContext) -> gitpython.Repo | str:
    """Return a GitPython Repo for the workspace git root, or an error string."""
    if ctx.workspace.git_root is None:
        return (
            "ERROR: workspace is not inside a git repository. "
            "Run 'git init' in the project root first."
        )
    try:
        return gitpython.Repo(str(ctx.workspace.git_root))
    except gitpython.InvalidGitRepositoryError as exc:
        return f"ERROR: invalid git repository at '{ctx.workspace.git_root}': {exc}"
    except gitpython.GitCommandNotFound:
        return "ERROR: git is not installed or not on PATH"


# ---------------------------------------------------------------------------
# git_status
# ---------------------------------------------------------------------------


class GitStatusParams(BaseModel):
    pass  # no parameters needed


class GitStatusTool:
    definition = ToolDef(
        name="git_status",
        description=(
            "Show the working tree status of the git repository "
            "(equivalent to `git status --porcelain`). "
            "Returns an error if the workspace is not inside a git repository."
        ),
        parameters=GitStatusParams,
    )
    requires_approval = False

    async def execute(self, args: dict[str, object], ctx: ToolContext) -> ToolResult:
        repo = _get_repo(ctx)
        if isinstance(repo, str):
            return ToolResult(tool_call_id="", content=repo, is_error=True)

        try:
            status = repo.git.status("--porcelain")
        except gitpython.GitCommandError as exc:
            return ToolResult(
                tool_call_id="",
                content=f"ERROR: git status failed: {exc}",
                is_error=True,
            )

        if not status:
            return ToolResult(
                tool_call_id="",
                content="(nothing to commit, working tree clean)",
                is_error=False,
            )
        return ToolResult(tool_call_id="", content=status, is_error=False)


# ---------------------------------------------------------------------------
# git_diff
# ---------------------------------------------------------------------------


class GitDiffParams(BaseModel):
    path: str | None = Field(
        default=None,
        description="Optional path (relative to workspace root) to diff",
    )
    staged: bool = Field(
        default=False,
        description="If true, show staged (cached) diff instead of working tree diff",
    )


class GitDiffTool:
    definition = ToolDef(
        name="git_diff",
        description=(
            "Show a unified diff of uncommitted changes. "
            "Use staged=true to see staged changes (`git diff --cached`). "
            "Optionally restrict to a specific path. "
            "Returns an error if not inside a git repository."
        ),
        parameters=GitDiffParams,
    )
    requires_approval = False

    async def execute(self, args: dict[str, object], ctx: ToolContext) -> ToolResult:
        params = GitDiffParams.model_validate(args)
        repo = _get_repo(ctx)
        if isinstance(repo, str):
            return ToolResult(tool_call_id="", content=repo, is_error=True)

        diff_args: list[str] = []
        if params.staged:
            diff_args.append("--cached")
        if params.path:
            # Validate path is inside workspace
            resolved = (ctx.workspace.root / params.path).resolve()
            if not ctx.workspace.is_path_inside(resolved):
                return ToolResult(
                    tool_call_id="",
                    content=(
                        f"ERROR: path '{params.path}' resolves outside workspace root "
                        f"({ctx.workspace.root})"
                    ),
                    is_error=True,
                )
            diff_args.append("--")
            diff_args.append(str(resolved))

        try:
            diff_output = repo.git.diff(*diff_args)
        except gitpython.GitCommandError as exc:
            return ToolResult(
                tool_call_id="",
                content=f"ERROR: git diff failed: {exc}",
                is_error=True,
            )

        if not diff_output:
            label = "staged changes" if params.staged else "working tree changes"
            return ToolResult(
                tool_call_id="",
                content=f"(no {label})",
                is_error=False,
            )
        return ToolResult(tool_call_id="", content=diff_output, is_error=False)


# ---------------------------------------------------------------------------
# git_commit
# ---------------------------------------------------------------------------


class GitCommitParams(BaseModel):
    message: str = Field(description="Commit message (must not be empty)")
    add_all: bool = Field(
        default=False,
        description=(
            "If true, stage all tracked modified files before committing "
            "(equivalent to `git add -u`). "
            "Untracked files are NOT staged automatically."
        ),
    )


class GitCommitTool:
    definition = ToolDef(
        name="git_commit",
        description=(
            "Create a git commit with the given message. "
            "Set add_all=true to stage all modified tracked files first. "
            "API key literals in the message are automatically redacted. "
            "Returns an error if there is nothing to commit or not in a git repo."
        ),
        parameters=GitCommitParams,
    )
    requires_approval = True

    async def execute(self, args: dict[str, object], ctx: ToolContext) -> ToolResult:
        params = GitCommitParams.model_validate(args)
        repo = _get_repo(ctx)
        if isinstance(repo, str):
            return ToolResult(tool_call_id="", content=repo, is_error=True)

        safe_message = _redact_secrets(params.message)
        if not safe_message.strip():
            return ToolResult(
                tool_call_id="",
                content="ERROR: commit message must not be empty",
                is_error=True,
            )

        try:
            if params.add_all:
                repo.git.add("-u")

            # Check if there is anything staged
            staged_diff = repo.git.diff("--cached", "--name-only")
            if not staged_diff.strip():
                return ToolResult(
                    tool_call_id="",
                    content=(
                        "ERROR: nothing staged to commit. "
                        "Use add_all=true or stage files manually first."
                    ),
                    is_error=True,
                )

            repo.git.commit("-m", safe_message)
        except gitpython.GitCommandError as exc:
            return ToolResult(
                tool_call_id="",
                content=f"ERROR: git commit failed: {exc}",
                is_error=True,
            )

        return ToolResult(
            tool_call_id="",
            content=f"OK: committed with message: {safe_message!r}",
            is_error=False,
        )
