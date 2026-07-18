"""Smoke test: every src/krodo subpackage must be importable.

This guards the Protocol-first principle (architecture.md §11.2) — if a
submodule fails to import, every downstream test will fail noisily, so we
catch it once here.

Replace with real tests as each module gets implemented in Phase 1.2+.
"""

from __future__ import annotations

import importlib

import pytest

EXPECTED_SUBMODULES = (
    "krodo",
    "krodo.cli",
    "krodo.core",
    "krodo.indexer",
    "krodo.llm",
    "krodo.memory",
    "krodo.obs",
    "krodo.sandbox",
    "krodo.tools",
)


@pytest.mark.parametrize("module_name", EXPECTED_SUBMODULES)
def test_subpackage_importable(module_name: str) -> None:
    """Each declared subpackage must import without side effects."""
    importlib.import_module(module_name)
