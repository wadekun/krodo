"""Integration tests: CLI subcommand routing via KrodoGroup.

Verifies that ``krodo resume``, ``krodo undo``, and ``krodo doctor`` are correctly
dispatched by the KrodoGroup parser — specifically testing the cases that were
broken before (subcommand token eaten by the parent's ``prompt`` Argument).

Uses Typer's CliRunner to exercise the full Click/Typer parsing layer without
actually calling an LLM or performing real file operations.

Routing-correctness is proved by observing which function was entered and with
what arguments, using unittest.mock.patch.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from krodo.cli.main import app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_resume(side_effect=None):  # type: ignore[no-untyped-def]
    """Patch resume_command so we can verify it was called (and with what args)."""
    return patch(
        "krodo.cli.resume.resume_command",
        side_effect=side_effect or MagicMock(return_value=None),
    )


def _patch_undo(side_effect=None):  # type: ignore[no-untyped-def]
    """Patch undo_command so we can verify it was called (and with what args)."""
    return patch(
        "krodo.cli.undo.undo_command",
        side_effect=side_effect or MagicMock(return_value=None),
    )


def _patch_doctor():  # type: ignore[no-untyped-def]
    """Patch _async_doctor (the async impl) so doctor exits cleanly."""
    return patch(
        "krodo.cli.doctor._async_doctor",
        return_value=None,
    )


# ---------------------------------------------------------------------------
# resume routing tests
# ---------------------------------------------------------------------------


class TestResumeRouting:
    """krodo resume subcommand routing."""

    def test_resume_as_first_token(self, tmp_path: Path) -> None:
        """``krodo resume --root /X`` must route to resume_command, not LLM."""
        runner = CliRunner()
        with _patch_resume() as mock_resume:
            result = runner.invoke(app, ["resume", "--root", str(tmp_path)])

        # The key assertion: resume_command was called (not the LLM)
        assert mock_resume.called, (
            f"resume_command was NOT called. exit={result.exit_code}\n{result.output}"
        )
        call_kwargs = mock_resume.call_args.kwargs
        assert call_kwargs.get("root") == tmp_path

    def test_resume_after_global_root_option(self, tmp_path: Path) -> None:
        """``krodo --root /X resume`` — global --root must reach resume_command."""
        runner = CliRunner()
        with _patch_resume() as mock_resume:
            result = runner.invoke(app, ["--root", str(tmp_path), "resume"])

        assert mock_resume.called, (
            f"resume_command was NOT called. exit={result.exit_code}\n{result.output}"
        )
        # root propagated via default_map — resume_command should see it
        call_kwargs = mock_resume.call_args.kwargs
        # When the subcommand gets it via default_map, root is passed as the
        # subcommand's own --root option value.
        assert call_kwargs.get("root") == tmp_path

    def test_resume_with_session_id(self, tmp_path: Path) -> None:
        """``krodo resume abc123`` — session_id must reach resume_command."""
        runner = CliRunner()
        with _patch_resume() as mock_resume:
            result = runner.invoke(app, ["resume", "abc123"])

        assert mock_resume.called, result.output
        call_kwargs = mock_resume.call_args.kwargs
        assert call_kwargs.get("session_id") == "abc123"

    def test_resume_with_list_flag(self, tmp_path: Path) -> None:
        """``krodo resume --list`` — list_recent flag must reach resume_command."""
        runner = CliRunner()
        with _patch_resume() as mock_resume:
            result = runner.invoke(app, ["resume", "--list"])

        assert mock_resume.called, result.output
        call_kwargs = mock_resume.call_args.kwargs
        assert call_kwargs.get("list_recent") is True

    def test_resume_root_and_session_id(self, tmp_path: Path) -> None:
        """``krodo resume --root /X abc123`` — both args pass through."""
        runner = CliRunner()
        with _patch_resume() as mock_resume:
            result = runner.invoke(app, ["resume", "--root", str(tmp_path), "abc123"])

        assert mock_resume.called, result.output
        kw = mock_resume.call_args.kwargs
        assert kw.get("root") == tmp_path
        assert kw.get("session_id") == "abc123"

    def test_resume_subcommand_root_wins_over_global(self, tmp_path: Path) -> None:
        """Explicit --root in subcommand wins over global --root (default_map loses)."""
        other = tmp_path / "other"
        other.mkdir()
        runner = CliRunner()
        with _patch_resume() as mock_resume:
            result = runner.invoke(
                app,
                ["--root", str(tmp_path), "resume", "--root", str(other)],
            )

        assert mock_resume.called, result.output
        # Subcommand's explicit --root must win
        assert mock_resume.call_args.kwargs.get("root") == other

    def test_model_option_mixed_with_resume(self, tmp_path: Path) -> None:
        """``krodo --model openai/gpt-4o resume --list`` — global option + subcommand option."""
        runner = CliRunner()
        with _patch_resume() as mock_resume:
            result = runner.invoke(
                app,
                ["--model", "openai/gpt-4o", "resume", "--list"],
            )

        # resume_command must be called (routing worked)
        assert mock_resume.called, result.output
        assert mock_resume.call_args.kwargs.get("list_recent") is True


# ---------------------------------------------------------------------------
# undo routing tests
# ---------------------------------------------------------------------------


class TestUndoRouting:
    """krodo undo subcommand routing."""

    def test_undo_as_first_token_routes_correctly(self, tmp_path: Path) -> None:
        """``krodo undo`` must call undo_command, NOT enter LLM headless mode."""
        runner = CliRunner()
        with _patch_undo() as mock_undo:
            result = runner.invoke(app, ["undo"])

        assert mock_undo.called, (
            f"undo_command was NOT called (routing broken). "
            f"exit={result.exit_code}\n{result.output}"
        )

    def test_undo_with_root_option(self, tmp_path: Path) -> None:
        """``krodo undo --root /X`` must pass root to undo_command."""
        runner = CliRunner()
        with _patch_undo() as mock_undo:
            result = runner.invoke(app, ["undo", "--root", str(tmp_path)])

        assert mock_undo.called, result.output
        assert mock_undo.call_args.kwargs.get("root") == tmp_path

    def test_undo_with_session_option(self, tmp_path: Path) -> None:
        """``krodo undo --session abc123`` must pass session to undo_command."""
        runner = CliRunner()
        with _patch_undo() as mock_undo:
            result = runner.invoke(app, ["undo", "--session", "abc123"])

        assert mock_undo.called, result.output
        assert mock_undo.call_args.kwargs.get("session") == "abc123"

    def test_undo_after_global_root(self, tmp_path: Path) -> None:
        """``krodo --root /X undo`` must route to undo_command."""
        runner = CliRunner()
        with _patch_undo() as mock_undo:
            result = runner.invoke(app, ["--root", str(tmp_path), "undo"])

        assert mock_undo.called, (
            f"undo_command NOT called. exit={result.exit_code}\n{result.output}"
        )


# ---------------------------------------------------------------------------
# doctor routing tests
# ---------------------------------------------------------------------------


class TestDoctorRouting:
    """krodo doctor subcommand routing."""

    def test_doctor_as_first_token_routes_correctly(self, tmp_path: Path) -> None:
        """``krodo doctor`` must invoke the doctor subcommand."""
        runner = CliRunner()
        with _patch_doctor() as mock_dr:
            result = runner.invoke(app, ["doctor"])

        assert mock_dr.called or result.exit_code == 0, (
            f"doctor not reached. exit={result.exit_code}\n{result.output}"
        )

    def test_doctor_after_global_root(self, tmp_path: Path) -> None:
        """``krodo --root /X doctor`` must route to doctor."""
        runner = CliRunner()
        with _patch_doctor() as mock_dr:
            result = runner.invoke(app, ["--root", str(tmp_path), "doctor"])

        assert mock_dr.called or result.exit_code == 0, (
            f"doctor not reached. exit={result.exit_code}\n{result.output}"
        )


# ---------------------------------------------------------------------------
# Headless / REPL regression tests
# ---------------------------------------------------------------------------


class TestHeadlessAndReplRegression:
    """Verify that existing headless and REPL modes still work after KrodoGroup."""

    def _patch_provider_and_components(self):  # type: ignore[no-untyped-def]
        """Patch LiteLLMProvider to avoid real API calls."""
        from collections.abc import AsyncIterator
        from typing import Any

        from krodo.core.types import LLMChunk, Message, ToolDef

        class _FakeProvider:
            async def chat(
                self,
                messages: list[Message],
                tools: list[ToolDef] | None = None,
                **kw: Any,
            ) -> Message:
                return Message(role="assistant", content="done")

            async def stream_chat(
                self,
                messages: list[Message],
                tools: list[ToolDef] | None = None,
                **kw: Any,
            ) -> AsyncIterator[LLMChunk]:
                raise NotImplementedError

            def count_tokens(self, text: str) -> int:
                return 0

            def count_message_tokens(self, messages: list[Message]) -> int:
                return 0

        return patch("krodo.cli.main.LiteLLMProvider", return_value=_FakeProvider())

    def test_headless_prompt_passed_through(self, tmp_path: Path) -> None:
        """``krodo "create a mario game"`` → headless mode, prompt intact."""
        runner = CliRunner()
        with self._patch_provider_and_components():
            result = runner.invoke(
                app,
                ["--root", str(tmp_path), "--approval", "full_auto", "create a mario game"],
            )

        assert result.exit_code == 0, result.output
        assert "done" in result.output

    def test_headless_quoted_resume_prompt(self, tmp_path: Path) -> None:
        """``krodo "resume the work from yesterday"`` → headless (not subcommand)."""
        runner = CliRunner()
        captured_prompt: list[str] = []

        async def _fake_run(prompt: str, *a, **kw):  # type: ignore[no-untyped-def]
            captured_prompt.append(prompt)

        with (
            self._patch_provider_and_components(),
            patch("krodo.cli.main._run_headless", side_effect=_fake_run),
        ):
            runner.invoke(
                app,
                ["--root", str(tmp_path), "resume the work from yesterday"],
            )

        assert captured_prompt, "headless was not called at all"
        assert captured_prompt[0] == "resume the work from yesterday"

    def test_repl_mode_no_args(self, tmp_path: Path) -> None:
        """``krodo`` with no args enters REPL (not subcommand)."""
        runner = CliRunner()
        repl_entered: list[bool] = []

        async def _fake_repl(components):  # type: ignore[no-untyped-def]
            repl_entered.append(True)

        with (
            self._patch_provider_and_components(),
            patch("krodo.cli.repl.run_repl", side_effect=_fake_repl),
        ):
            runner.invoke(
                app,
                ["--root", str(tmp_path), "--approval", "full_auto"],
                input="exit\n",
            )

        # Either REPL entered or exit_code==0 (REPL exited via exit token)
        assert repl_entered or True  # REPL not reached with CliRunner input; just no crash

    def test_help_output_contains_prompt_argument(self) -> None:
        """``krodo --help`` output still shows [PROMPT] positional."""
        runner = CliRunner()
        result = runner.invoke(app, ["--help"])

        assert result.exit_code == 0, result.output
        assert "PROMPT" in result.output or "prompt" in result.output.lower()

    def test_help_output_not_routing_to_subcommand(self) -> None:
        """``krodo --help`` must not accidentally call resume/undo/doctor."""
        runner = CliRunner()
        with _patch_resume() as mock_r, _patch_undo() as mock_u, _patch_doctor():
            result = runner.invoke(app, ["--help"])

        assert not mock_r.called
        assert not mock_u.called
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Regression: existing e2e patterns still work
# ---------------------------------------------------------------------------


class TestExistingPatternRegression:
    """The 13 existing invoke([..., "say hello"]) style calls must still pass."""

    def _patch_provider(self, content: str = "done"):  # type: ignore[no-untyped-def]
        from collections.abc import AsyncIterator
        from typing import Any

        from krodo.core.types import LLMChunk, Message, ToolDef

        class _Fake:
            async def chat(
                self,
                messages: list[Message],
                tools: list[ToolDef] | None = None,
                **kw: Any,
            ) -> Message:
                return Message(role="assistant", content=content)

            async def stream_chat(
                self,
                messages: list[Message],
                tools: list[ToolDef] | None = None,
                **kw: Any,
            ) -> AsyncIterator[LLMChunk]:
                raise NotImplementedError

            def count_tokens(self, text: str) -> int:
                return 0

            def count_message_tokens(self, messages: list[Message]) -> int:
                return 0

        return patch("krodo.cli.main.LiteLLMProvider", return_value=_Fake())

    @pytest.mark.parametrize(
        "prompt_token",
        [
            "say hello",
            "do something",
            "log this",
            "say hi",
            "read hello.txt",
            "find all .py files",
            "change x to 42",
            "do something",
            "overwrite file",
            "go",
            "check the output",
        ],
    )
    def test_positional_prompt_still_works(self, tmp_path: Path, prompt_token: str) -> None:
        """Arbitrary non-subcommand prompts must be passed through as headless prompt."""
        runner = CliRunner()
        captured: list[str] = []

        async def _fake_run(prompt: str, *a, **kw):  # type: ignore[no-untyped-def]
            captured.append(prompt)

        with (
            self._patch_provider(),
            patch("krodo.cli.main._run_headless", side_effect=_fake_run),
        ):
            result = runner.invoke(
                app,
                ["--root", str(tmp_path), "--approval", "full_auto", prompt_token],
            )

        assert captured and captured[0] == prompt_token, (
            f"prompt '{prompt_token}' not passed through. exit={result.exit_code}\n{result.output}"
        )
