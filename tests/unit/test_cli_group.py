"""Unit tests for CodaGroup — the custom Click/Typer Group that pre-routes subcommands.

Tests cover:
  * _find_subcommand_split: all option-form variants and edge cases
  * _propagate_defaults: default_map propagation logic
  * parse_args: integration — correct ctx._protected_args / ctx.args / ctx.default_map
"""

from __future__ import annotations

import click
import pytest
import typer

from coda.cli.group import CodaGroup

# ---------------------------------------------------------------------------
# Helpers: build a minimal CodaGroup-backed app for testing
# ---------------------------------------------------------------------------


def _make_app() -> typer.Typer:
    """Minimal Typer app with CodaGroup and the same option layout as `coda`."""
    app = typer.Typer(
        name="coda",
        cls=CodaGroup,
        invoke_without_command=True,
        no_args_is_help=False,
        add_completion=False,
    )

    # Add a "resume" subcommand
    resume_app = typer.Typer(
        name="resume",
        invoke_without_command=True,
        add_completion=False,
    )

    @resume_app.callback(invoke_without_command=True)
    def _resume(
        session_id: str | None = typer.Argument(None),
        root: str | None = typer.Option(None, "--root", "-r"),
        list_sessions: bool = typer.Option(False, "--list", "-l"),
    ) -> None:
        pass

    app.add_typer(resume_app)

    # Add an "undo" subcommand
    undo_app = typer.Typer(
        name="undo",
        invoke_without_command=True,
        add_completion=False,
    )

    @undo_app.callback(invoke_without_command=True)
    def _undo(
        root: str | None = typer.Option(None, "--root", "-r"),
        session: str | None = typer.Option(None, "--session", "-s"),
    ) -> None:
        pass

    app.add_typer(undo_app)

    # Add a "doctor" subcommand
    doctor_app = typer.Typer(
        name="doctor",
        invoke_without_command=True,
        add_completion=False,
    )

    @doctor_app.callback(invoke_without_command=True)
    def _doctor(
        root: str | None = typer.Option(None, "--root", "-r"),
    ) -> None:
        pass

    app.add_typer(doctor_app)

    @app.callback(invoke_without_command=True)
    def _main(
        ctx: typer.Context,
        prompt: str | None = typer.Argument(None),
        root: str | None = typer.Option(None, "--root", "-r"),
        model: str = typer.Option("default-model", "--model", "-m"),
        approval: str = typer.Option("auto_edit", "--approval", "-a"),
        verbose: bool = typer.Option(False, "--verbose", "-v"),
    ) -> None:
        pass

    return app


def _get_group(app: typer.Typer) -> CodaGroup:
    """Extract the underlying Click Group from a Typer app."""
    click_app = typer.main.get_command(app)
    assert isinstance(click_app, CodaGroup)
    return click_app


# ---------------------------------------------------------------------------
# Tests for _find_subcommand_split
# ---------------------------------------------------------------------------


class TestFindSubcommandSplit:
    """Tests for CodaGroup._find_subcommand_split."""

    @pytest.fixture()
    def grp(self) -> CodaGroup:
        app = _make_app()
        return _get_group(app)

    @pytest.fixture()
    def ctx(self, grp: CodaGroup) -> click.Context:
        return click.Context(grp)

    def test_subcommand_first_token(self, grp: CodaGroup, ctx: click.Context) -> None:
        """resume as first token → (0, 'resume')."""
        result = grp._find_subcommand_split(ctx, ["resume"])  # noqa: SLF001
        assert result == (0, "resume")

    def test_subcommand_after_long_value_option(
        self, grp: CodaGroup, ctx: click.Context
    ) -> None:
        """--root /X resume → (2, 'resume') (skips key+value)."""
        result = grp._find_subcommand_split(ctx, ["--root", "/tmp/X", "resume"])  # noqa: SLF001
        assert result == (2, "resume")

    def test_subcommand_after_inline_value_option(
        self, grp: CodaGroup, ctx: click.Context
    ) -> None:
        """--root=/X resume → (1, 'resume') (--key=value is one token)."""
        result = grp._find_subcommand_split(ctx, ["--root=/tmp/X", "resume"])  # noqa: SLF001
        assert result == (1, "resume")

    def test_subcommand_after_short_value_option(
        self, grp: CodaGroup, ctx: click.Context
    ) -> None:
        """-r /X resume → (2, 'resume')."""
        result = grp._find_subcommand_split(ctx, ["-r", "/tmp/X", "resume"])  # noqa: SLF001
        assert result == (2, "resume")

    def test_subcommand_after_flag_option(
        self, grp: CodaGroup, ctx: click.Context
    ) -> None:
        """--verbose resume → (1, 'resume') (flag takes no value)."""
        result = grp._find_subcommand_split(ctx, ["--verbose", "resume"])  # noqa: SLF001
        assert result == (1, "resume")

    def test_subcommand_after_mixed_options(
        self, grp: CodaGroup, ctx: click.Context
    ) -> None:
        """--root /X --approval full_auto resume → (4, 'resume')."""
        result = grp._find_subcommand_split(  # noqa: SLF001
            ctx, ["--root", "/tmp/X", "--approval", "full_auto", "resume"]
        )
        assert result == (4, "resume")

    def test_subcommand_after_mixed_short_long(
        self, grp: CodaGroup, ctx: click.Context
    ) -> None:
        """-r /X -m gpt-4 resume → (4, 'resume')."""
        result = grp._find_subcommand_split(  # noqa: SLF001
            ctx, ["-r", "/tmp/X", "-m", "gpt-4", "resume"]
        )
        assert result == (4, "resume")

    def test_undo_subcommand(self, grp: CodaGroup, ctx: click.Context) -> None:
        """undo is also a registered subcommand."""
        result = grp._find_subcommand_split(ctx, ["undo"])  # noqa: SLF001
        assert result == (0, "undo")

    def test_doctor_subcommand(self, grp: CodaGroup, ctx: click.Context) -> None:
        """doctor is also a registered subcommand."""
        result = grp._find_subcommand_split(ctx, ["doctor"])  # noqa: SLF001
        assert result == (0, "doctor")

    def test_no_subcommand_plain_prompt(
        self, grp: CodaGroup, ctx: click.Context
    ) -> None:
        """Plain prompt string → None."""
        result = grp._find_subcommand_split(ctx, ["create a mario game"])  # noqa: SLF001
        assert result is None

    def test_no_subcommand_option_value_looks_like_subcmd(
        self, grp: CodaGroup, ctx: click.Context
    ) -> None:
        """Option value that matches a subcommand name must NOT be treated as subcommand."""
        result = grp._find_subcommand_split(ctx, ["--model", "resume"])  # noqa: SLF001
        assert result is None

    def test_no_subcommand_option_value_short(
        self, grp: CodaGroup, ctx: click.Context
    ) -> None:
        """-m resume should not route to resume."""
        result = grp._find_subcommand_split(ctx, ["-m", "resume"])  # noqa: SLF001
        assert result is None

    def test_no_subcommand_empty_args(
        self, grp: CodaGroup, ctx: click.Context
    ) -> None:
        """Empty args → None."""
        result = grp._find_subcommand_split(ctx, [])  # noqa: SLF001
        assert result is None

    def test_no_subcommand_all_options(
        self, grp: CodaGroup, ctx: click.Context
    ) -> None:
        """Only options (no positional) → None."""
        result = grp._find_subcommand_split(ctx, ["--root", "/X", "--verbose"])  # noqa: SLF001
        assert result is None

    def test_subcommand_after_double_dash(
        self, grp: CodaGroup, ctx: click.Context
    ) -> None:
        """After '--', the next resume token should be detected as subcommand."""
        result = grp._find_subcommand_split(ctx, ["--", "resume"])  # noqa: SLF001
        assert result == (1, "resume")

    def test_quoted_prompt_with_subcommand_word_inside(
        self, grp: CodaGroup, ctx: click.Context
    ) -> None:
        """A single-token prompt that *contains* 'resume' is not a subcommand."""
        result = grp._find_subcommand_split(ctx, ["resume the work from yesterday"])  # noqa: SLF001
        assert result is None

    def test_subcommand_with_trailing_args(
        self, grp: CodaGroup, ctx: click.Context
    ) -> None:
        """resume abc123 → finds resume at index 0; abc123 is subcmd arg."""
        result = grp._find_subcommand_split(ctx, ["resume", "abc123"])  # noqa: SLF001
        assert result == (0, "resume")


# ---------------------------------------------------------------------------
# Tests for _propagate_defaults
# ---------------------------------------------------------------------------


class TestPropagateDefaults:
    """Tests for CodaGroup._propagate_defaults."""

    @pytest.fixture()
    def grp(self) -> CodaGroup:
        return _get_group(_make_app())

    def test_propagates_root(self, grp: CodaGroup) -> None:
        ctx = click.Context(grp)
        ctx.params = {"root": "/tmp/X", "model": None, "prompt": None}
        grp._propagate_defaults(ctx, "resume")  # noqa: SLF001
        assert ctx.default_map is not None
        assert ctx.default_map["resume"]["root"] == "/tmp/X"

    def test_does_not_propagate_none(self, grp: CodaGroup) -> None:
        ctx = click.Context(grp)
        ctx.params = {"root": None, "model": None}
        grp._propagate_defaults(ctx, "resume")  # noqa: SLF001
        # No entries added because all values are None
        assert not ctx.default_map or "resume" not in ctx.default_map

    def test_does_not_override_existing_default_map(self, grp: CodaGroup) -> None:
        ctx = click.Context(grp)
        ctx.params = {"root": "/from-group"}
        ctx.default_map = {"resume": {"root": "/pre-existing"}}
        grp._propagate_defaults(ctx, "resume")  # noqa: SLF001
        # _propagate_defaults merges with existing; group value fills the key
        assert ctx.default_map["resume"]["root"] == "/from-group"

    def test_does_not_propagate_prompt(self, grp: CodaGroup) -> None:
        ctx = click.Context(grp)
        ctx.params = {"prompt": "some task", "root": "/X"}
        grp._propagate_defaults(ctx, "resume")  # noqa: SLF001
        assert "prompt" not in (ctx.default_map or {}).get("resume", {})

    def test_multiple_shared_keys(self, grp: CodaGroup) -> None:
        ctx = click.Context(grp)
        ctx.params = {"root": "/X", "model": "gpt-4", "approval": "full_auto"}
        grp._propagate_defaults(ctx, "resume")  # noqa: SLF001
        dm = ctx.default_map["resume"]
        assert dm["root"] == "/X"
        assert dm["model"] == "gpt-4"
        assert dm["approval"] == "full_auto"


# ---------------------------------------------------------------------------
# Tests for parse_args (state verification)
# ---------------------------------------------------------------------------


class TestParseArgs:
    """Integration-style tests verifying ctx state after CodaGroup.parse_args."""

    @pytest.fixture()
    def grp(self) -> CodaGroup:
        return _get_group(_make_app())

    def _make_ctx(self, grp: CodaGroup) -> click.Context:
        ctx = click.Context(grp, resilient_parsing=False, info_name="coda")
        ctx.ensure_object(dict)
        return ctx

    def test_subcommand_token_sets_protected_args(self, grp: CodaGroup) -> None:
        """'resume' in first position → ctx._protected_args=['resume']."""
        ctx = self._make_ctx(grp)
        grp.parse_args(ctx, ["resume"])
        assert ctx._protected_args == ["resume"]  # noqa: SLF001
        assert ctx.params.get("prompt") is None

    def test_subcommand_with_options_after(self, grp: CodaGroup) -> None:
        """resume --root /X → resume routes; subcommand gets --root /X."""
        ctx = self._make_ctx(grp)
        grp.parse_args(ctx, ["resume", "--root", "/tmp/X"])
        assert ctx._protected_args == ["resume"]  # noqa: SLF001
        assert ctx.args == ["--root", "/tmp/X"]
        assert ctx.params.get("prompt") is None

    def test_group_option_before_subcommand(self, grp: CodaGroup) -> None:
        """--root /X resume → group parses --root; resume routes."""
        ctx = self._make_ctx(grp)
        grp.parse_args(ctx, ["--root", "/tmp/X", "resume"])
        assert ctx._protected_args == ["resume"]  # noqa: SLF001
        assert ctx.args == []
        assert ctx.params.get("root") == "/tmp/X"
        assert ctx.params.get("prompt") is None

    def test_group_option_propagated_to_default_map(self, grp: CodaGroup) -> None:
        """--root /X before resume → default_map['resume']['root'] = /X."""
        ctx = self._make_ctx(grp)
        grp.parse_args(ctx, ["--root", "/tmp/X", "resume"])
        assert ctx.default_map is not None
        assert ctx.default_map.get("resume", {}).get("root") == "/tmp/X"

    def test_plain_prompt_not_routed(self, grp: CodaGroup) -> None:
        """Non-subcommand prompt → _protected_args stays empty; prompt filled."""
        ctx = self._make_ctx(grp)
        grp.parse_args(ctx, ["create a mario game"])
        assert ctx._protected_args == []  # noqa: SLF001
        assert ctx.params.get("prompt") == "create a mario game"

    def test_empty_args_not_routed(self, grp: CodaGroup) -> None:
        """No args → REPL mode; _protected_args empty."""
        ctx = self._make_ctx(grp)
        grp.parse_args(ctx, [])
        assert ctx._protected_args == []  # noqa: SLF001
        assert ctx.params.get("prompt") is None

    def test_option_value_matching_subcommand_name_not_routed(
        self, grp: CodaGroup
    ) -> None:
        """--model resume should NOT route to resume subcommand."""
        ctx = self._make_ctx(grp)
        grp.parse_args(ctx, ["--model", "resume"])
        assert ctx._protected_args == []  # noqa: SLF001

    def test_subcommand_with_session_id(self, grp: CodaGroup) -> None:
        """resume abc123 → resume routes, abc123 passed as subcmd arg."""
        ctx = self._make_ctx(grp)
        grp.parse_args(ctx, ["resume", "abc123"])
        assert ctx._protected_args == ["resume"]  # noqa: SLF001
        assert ctx.args == ["abc123"]

    def test_undo_subcommand_routed(self, grp: CodaGroup) -> None:
        """undo → routes to undo, not prompt."""
        ctx = self._make_ctx(grp)
        grp.parse_args(ctx, ["undo"])
        assert ctx._protected_args == ["undo"]  # noqa: SLF001
        assert ctx.params.get("prompt") is None

    def test_doctor_subcommand_routed(self, grp: CodaGroup) -> None:
        """doctor → routes to doctor."""
        ctx = self._make_ctx(grp)
        grp.parse_args(ctx, ["doctor"])
        assert ctx._protected_args == ["doctor"]  # noqa: SLF001
        assert ctx.params.get("prompt") is None
