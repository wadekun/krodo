"""Krodo config file loading — workspace YAML + user TOML (M5.4).

Precedence (highest wins):
  1. CLI flag
  2. Environment variable
  3. Workspace config  ``<workspace>/.krodo/config.yaml``
  4. User config       ``~/.config/krodo/config.toml``
  5. Built-in default

Usage::

    cfg = load_config(workspace.root)
    # Then in main() callback, if a flag is still at its default value,
    # replace it with cfg.<name> when cfg.<name> is not None.

Both config files are optional — missing files are silently ignored.
Schema errors log a warning and return an empty (all-None) config.
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError, field_validator

from krodo.core.types import ApprovalMode

logger = logging.getLogger(__name__)


# Backends recognised by the symbol index (M9). ``lsp`` is intentionally NOT in
# this set — it is a Phase 3 backend, rejected up-front with a friendly message.
_KNOWN_SYMBOL_BACKENDS = frozenset({"treesitter", "off"})


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class KrodoConfig(BaseModel):
    """Merged configuration from all file-based sources.

    Only fields explicitly set in a config file will be non-None.  None means
    "not configured here — use next-priority source".
    """

    model: str | None = None
    api_base: str | None = None
    approval: ApprovalMode | None = None
    max_tokens: int | None = None
    max_tool_calls: int | None = None
    summary_window: int | None = None
    compress: Literal["llm", "algorithmic"] | None = None
    # Anthropic prompt caching (tags the system message with cache_control
    # so the static prompt prefix is cached across turns). Defaults to True
    # when unset here; set to False in config.yaml to disable for cases
    # where cache-write cost outweighs the benefit (very short sessions).
    prompt_cache: bool | None = None
    # Symbol index backend (M9). Accepts a scalar or a list so the schema is
    # ready for the Phase 3 fallback chain (e.g. ``[lsp, treesitter]``). M9
    # only implements ``treesitter``; ``off`` disables indexing. See
    # ``resolve_symbol_backend`` for the canonical decision.
    symbol_backend: str | list[str] | None = None

    @field_validator("symbol_backend")
    @classmethod
    def _check_symbol_backend(cls, v: str | list[str] | None) -> str | list[str] | None:
        if v is None:
            return v
        values = list(v) if isinstance(v, list) else [v]
        if not values:
            raise ValueError("symbol_backend: empty list; legal values: treesitter, off")
        unique = set(values)
        if "lsp" in unique:
            raise ValueError(
                "symbol_backend 'lsp' is a Phase 3 backend (not yet implemented); "
                "current options: treesitter, off"
            )
        unknown = unique - _KNOWN_SYMBOL_BACKENDS
        if unknown:
            raise ValueError(
                f"symbol_backend: unknown value(s) {sorted(unknown)}; legal: treesitter, off"
            )
        if len(unique) > 1:
            raise ValueError("symbol_backend: cannot combine treesitter and off; choose one")
        return v


def resolve_symbol_backend(value: str | list[str] | None) -> str:
    """Reduce a validated ``symbol_backend`` value to ``"treesitter"`` or ``"off"``.

    Unset (``None``) defaults to ``"treesitter"`` — the index is on out of the
    box. The value is assumed already validated by :class:`KrodoConfig`.
    """
    if value is None:
        return "treesitter"
    values = set(value) if isinstance(value, list) else {value}
    return "off" if values == {"off"} else "treesitter"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_config(workspace_root: Path) -> tuple[KrodoConfig, list[str]]:
    """Merge user config ← workspace config and return (config, sources).

    Parameters
    ----------
    workspace_root:
        The resolved project root (``Workspace.root``).

    Returns
    -------
    config:
        Merged :class:`KrodoConfig` with only explicitly-set fields non-None.
    sources:
        Human-readable list of config files that were found and loaded, in
        priority order (highest-priority last wins over earlier entries).
        Used by ``krodo doctor`` to display which files contributed.
    """
    user_path = Path.home() / ".config" / "krodo" / "config.toml"
    workspace_path = workspace_root / ".krodo" / "config.yaml"

    user_data = _load_toml(user_path)
    workspace_data = _load_yaml(workspace_path)

    # Merge: user is the base; workspace values override user values
    merged: dict[str, object] = {}
    sources: list[str] = []

    if user_data is not None:
        merged.update(user_data)
        sources.append(str(user_path))

    if workspace_data is not None:
        merged.update(workspace_data)
        sources.append(str(workspace_path))

    if not merged:
        return KrodoConfig(), sources

    try:
        cfg = KrodoConfig.model_validate(merged)
    except ValidationError as exc:
        logger.warning("Config schema error (using defaults): %s", exc)
        return KrodoConfig(), sources

    return cfg, sources


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _load_toml(path: Path) -> dict[str, object] | None:
    """Load a TOML file; return None if missing, warn on parse error."""
    if not path.exists():
        return None
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not parse config %s: %s", path, exc)
        return None


def _load_yaml(path: Path) -> dict[str, object] | None:
    """Load a YAML file; return None if missing, warn on parse error."""
    if not path.exists():
        return None
    try:
        import yaml  # noqa: PLC0415

        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if data is None:
            return {}
        if not isinstance(data, dict):
            logger.warning("Config %s must be a mapping (got %s), ignoring.", path, type(data))
            return None
        return data
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not parse config %s: %s", path, exc)
        return None
