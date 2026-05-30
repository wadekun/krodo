"""Coda config file loading — workspace YAML + user TOML (M5.4).

Precedence (highest wins):
  1. CLI flag
  2. Environment variable
  3. Workspace config  ``<workspace>/.coda/config.yaml``
  4. User config       ``~/.config/coda/config.toml``
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

from pydantic import BaseModel, ValidationError

from coda.core.types import ApprovalMode

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class CodaConfig(BaseModel):
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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_config(workspace_root: Path) -> tuple[CodaConfig, list[str]]:
    """Merge user config ← workspace config and return (config, sources).

    Parameters
    ----------
    workspace_root:
        The resolved project root (``Workspace.root``).

    Returns
    -------
    config:
        Merged :class:`CodaConfig` with only explicitly-set fields non-None.
    sources:
        Human-readable list of config files that were found and loaded, in
        priority order (highest-priority last wins over earlier entries).
        Used by ``coda doctor`` to display which files contributed.
    """
    user_path = Path.home() / ".config" / "coda" / "config.toml"
    workspace_path = workspace_root / ".coda" / "config.yaml"

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
        return CodaConfig(), sources

    try:
        cfg = CodaConfig.model_validate(merged)
    except ValidationError as exc:
        logger.warning("Config schema error (using defaults): %s", exc)
        return CodaConfig(), sources

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
        return data  # type: ignore[return-value]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not parse config %s: %s", path, exc)
        return None
