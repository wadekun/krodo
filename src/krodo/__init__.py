"""Krodo — a local-first, multi-provider coding agent CLI."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("krodo")
except PackageNotFoundError:  # not installed (running from source without `uv pip install -e .`)
    __version__ = "dev"

__all__ = ["__version__"]
