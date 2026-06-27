"""KrodoGroup — custom Click/Typer Group that pre-routes subcommands.

PROBLEM
-------
The main Typer app declares both a positional ``prompt`` Argument (for
``krodo "task"`` headless mode) and three sub-apps (``undo`` / ``resume`` /
``doctor``).  Click's parser consumes positional Arguments *before* it
dispatches subcommands, so a token like ``resume`` is silently swallowed as
``prompt`` and never reaches the subcommand router.  This causes:

  * ``krodo resume --root /X``  → "No such command '--root'" error
  * ``krodo --root /X resume``  → silent failure (LLM gets prompt="resume")

FIX: args-split strategy
------------------------
Override ``parse_args`` to scan *args* for the first token that is both
(a) not an option or its value, and (b) a registered subcommand name.  When
found, split at that index:

  * *group_args* (everything before the subcommand token) → parsed by
    ``click.Command.parse_args``, which fills group-level options and leaves
    the ``prompt`` Argument at its default ``None``.
  * *subcmd_args* (everything after the subcommand token) → assigned to
    ``ctx.args``; the subcommand name goes into ``ctx._protected_args`` so
    Click's invocation machinery dispatches it correctly.

FOOT-GUN FIX: --root propagation
---------------------------------
When the user writes ``krodo --root /X resume``, the group-level ``--root``
ends up in *group_args* and is parsed onto the group ctx, but the ``resume``
subcommand callback declares its own ``--root`` which defaults to ``None``.
``_propagate_defaults`` copies shared group options into ``ctx.default_map``
so the subcommand sees them as defaults (but its own explicit ``--root`` still
wins).

RISKS
-----
* ``ctx._protected_args`` is a Click 8.x internal (underscore-prefixed).
  The public ``ctx.protected_args`` property is deprecated in 8.x and will be
  removed in Click 9.0.  This file should be audited when upgrading Click.
  Pin ``click>=8,<9`` in pyproject.toml.
* ``default_map`` only fills values that are still at the option's built-in
  default; an explicit CLI flag in the subcommand always takes precedence.
"""

from __future__ import annotations

from typing import Any

import click
from typer.core import TyperGroup


class KrodoGroup(TyperGroup):
    """TyperGroup subclass that correctly routes Krodo's named subcommands.

    WARN: depends on Click 8.x internals (ctx._protected_args).
    Review this file when upgrading click past 8.x.
    """

    # ------------------------------------------------------------------
    # Public override
    # ------------------------------------------------------------------

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        """Split args at the first subcommand token, if any."""
        if args and self.commands:
            split = self._find_subcommand_split(ctx, args)
            if split is not None:
                cmd_index, cmd_name = split
                group_args = list(args[:cmd_index])
                subcmd_args = list(args[cmd_index + 1 :])

                # Only parse group-level options — skips Group.parse_args's
                # automatic ``rest[:1]`` assignment that re-runs subcommand
                # dispatch based on leftover positional tokens.
                click.Command.parse_args(self, ctx, group_args)

                # Manually wire Click's subcommand dispatch mechanism.
                # WARN: _protected_args is a Click 8.x internal.
                ctx._protected_args = [cmd_name]  # noqa: SLF001
                ctx.args = subcmd_args

                # Propagate shared group options as subcommand defaults.
                self._propagate_defaults(ctx, cmd_name)

                return ctx.args

        return super().parse_args(ctx, args)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_subcommand_split(
        self,
        ctx: click.Context,
        args: list[str],
    ) -> tuple[int, str] | None:
        """Return *(index, name)* of the first subcommand token in *args*.

        Correctly skips over option tokens and their values so that option
        *values* that happen to match a subcommand name (e.g.
        ``--model resume``) are not misidentified as subcommands.

        Returns ``None`` if no subcommand token is found.
        """
        # Build a lookup of all long and short option strings → is_flag
        flag_opts: set[str] = set()
        value_opts: set[str] = set()
        for param in self.get_params(ctx):
            if not isinstance(param, click.Option):
                continue
            if param.is_flag or param.is_eager:
                flag_opts.update(param.opts)
            else:
                value_opts.update(param.opts)

        known_subcmds = set(self.commands.keys())

        i = 0
        skip_next = False  # True when the previous token was a value-taking option
        while i < len(args):
            tok = args[i]

            if skip_next:
                # This token is the *value* for the previous option — skip it.
                skip_next = False
                i += 1
                continue

            if tok == "--":
                # End of options; everything after is positional.
                # Return the first positional after "--" if it is a subcommand.
                for j in range(i + 1, len(args)):
                    if args[j] in known_subcmds:
                        return j, args[j]
                return None

            if tok.startswith("--"):
                if "=" in tok:
                    # ``--key=value`` — inline value, no next token consumed.
                    i += 1
                    continue
                opt_name = tok
                if opt_name in value_opts:
                    # ``--key value`` — next token is the value.
                    skip_next = True
                    i += 1
                    continue
                if opt_name in flag_opts:
                    # Boolean flag — no value token.
                    i += 1
                    continue
                # Unknown long option (e.g. ``--help``) — treat as flag.
                i += 1
                continue

            if tok.startswith("-") and len(tok) > 1:
                # Short option: ``-r``, ``-rv``, ``-r /path``
                short = tok[:2]  # e.g. ``-r``
                if short in value_opts:
                    if len(tok) > 2:
                        # Value is glued: ``-r/path`` — no next token consumed.
                        i += 1
                        continue
                    # Value is the next token.
                    skip_next = True
                    i += 1
                    continue
                # Boolean short or unknown: skip.
                i += 1
                continue

            # Non-option token — candidate for subcommand or positional.
            if tok in known_subcmds:
                return i, tok
            # It's a positional (the ``prompt`` Argument) — stop looking.
            return None

        return None

    def _propagate_defaults(self, ctx: click.Context, cmd_name: str) -> None:
        """Copy shared group options into ``ctx.default_map`` for *cmd_name*.

        This allows ``krodo --root /X resume`` to work correctly: the group
        parses ``--root /X`` onto its own ctx, and this method makes ``/X``
        available to the ``resume`` subcommand as a default (its own explicit
        ``--root`` still takes priority because Click only consults
        ``default_map`` when the option is still at its built-in default).
        """
        shared_keys = frozenset({"root", "model", "api_key", "api_base", "approval", "max_tokens"})
        inherited: dict[str, Any] = {
            k: v for k, v in ctx.params.items() if k in shared_keys and v is not None
        }
        if not inherited:
            return
        existing: dict[str, Any] = (ctx.default_map or {}).get(cmd_name, {})
        ctx.default_map = {
            **(ctx.default_map or {}),
            cmd_name: {**existing, **inherited},
        }
