"""Tests for sandbox/path_filter.py helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from coda.core.workspace import LocalWorkspaceResolver
from coda.sandbox.path_filter import _NOISE_DIRS, filter_allowed_paths, is_noise_dir

# ---------------------------------------------------------------------------
# is_noise_dir
# ---------------------------------------------------------------------------


def test_is_noise_dir_detects_noise_names() -> None:
    for name in _NOISE_DIRS:
        p = Path("project") / name / "file.py"
        assert is_noise_dir(p), f"Expected {name} to be detected as noise"


def test_is_noise_dir_passes_clean_paths() -> None:
    clean = [
        Path("src/coda/core/workspace.py"),
        Path("tests/unit/test_foo.py"),
        Path("README.md"),
    ]
    for p in clean:
        assert not is_noise_dir(p), f"Unexpected noise detection for {p}"


def test_is_noise_dir_nested_noise() -> None:
    p = Path("src/__pycache__/module.cpython-312.pyc")
    assert is_noise_dir(p)


# ---------------------------------------------------------------------------
# filter_allowed_paths
# ---------------------------------------------------------------------------


def test_filter_allowed_paths_keeps_inside(tmp_path: Path) -> None:
    f = tmp_path / "src" / "main.py"
    f.parent.mkdir()
    f.write_text("x = 1")
    ws = LocalWorkspaceResolver().resolve(explicit=tmp_path)
    result = filter_allowed_paths([f], ws)
    assert len(result) == 1
    assert result[0] == f.resolve()


def test_filter_allowed_paths_drops_outside(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside_filter"
    outside.mkdir(exist_ok=True)
    (outside / "file.py").write_text("x")
    ws = LocalWorkspaceResolver().resolve(explicit=tmp_path)
    result = filter_allowed_paths([outside / "file.py"], ws)
    assert result == []


def test_filter_allowed_paths_drops_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside_sym_filter"
    outside.mkdir(exist_ok=True)
    (outside / "secret.py").write_text("secret")
    link = tmp_path / "link.py"
    link.symlink_to(outside / "secret.py")
    ws = LocalWorkspaceResolver().resolve(explicit=tmp_path)
    result = filter_allowed_paths([link], ws)
    assert result == []


def test_filter_allowed_paths_drops_noise_dirs(tmp_path: Path) -> None:
    noise = tmp_path / "node_modules" / "pkg" / "index.js"
    noise.parent.mkdir(parents=True)
    noise.write_text("module.exports = {}")
    ws = LocalWorkspaceResolver().resolve(explicit=tmp_path)
    result = filter_allowed_paths([noise], ws)
    assert result == []


def test_filter_allowed_paths_mixed(tmp_path: Path) -> None:
    clean = tmp_path / "src" / "main.py"
    clean.parent.mkdir()
    clean.write_text("x = 1")
    noisy = tmp_path / "__pycache__" / "main.pyc"
    noisy.parent.mkdir()
    noisy.write_bytes(b"\x00")
    outside = tmp_path.parent / "outside_mixed"
    outside.mkdir(exist_ok=True)
    (outside / "evil.py").write_text("evil")

    ws = LocalWorkspaceResolver().resolve(explicit=tmp_path)
    result = filter_allowed_paths([clean, noisy, outside / "evil.py"], ws)
    assert len(result) == 1
    assert result[0] == clean.resolve()


@pytest.mark.parametrize("noise_name", list(_NOISE_DIRS)[:5])
def test_filter_rejects_all_noise_dir_variants(tmp_path: Path, noise_name: str) -> None:
    noise_dir = tmp_path / noise_name
    noise_dir.mkdir()
    f = noise_dir / "file.py"
    f.write_text("x")
    ws = LocalWorkspaceResolver().resolve(explicit=tmp_path)
    result = filter_allowed_paths([f], ws)
    assert result == [], f"Expected {noise_name} to be filtered out"
