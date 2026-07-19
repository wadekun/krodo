"""Tests for krodo.indexer.canary — subprocess canary probe (M9 closeout).

Covers ``sample_files`` (ignore obedience + early stop at ``sample_size``),
``probe`` (success / crash / timeout via a mocked ``subprocess.run``), and the
``_main`` subprocess entry point (never raises, skips unreadable paths).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from krodo.indexer import canary
from krodo.sandbox.ignore import KrodoIgnore

# ---------------------------------------------------------------------------
# sample_files
# ---------------------------------------------------------------------------


def test_sample_files_collects_supported_files(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def a():\n    pass\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def b():\n    pass\n", encoding="utf-8")
    (tmp_path / "c.txt").write_text("not source", encoding="utf-8")

    files = canary.sample_files(tmp_path, KrodoIgnore(tmp_path))

    names = {p.name for p in files}
    assert names == {"a.py", "b.py"}


def test_sample_files_stops_at_sample_size(tmp_path: Path) -> None:
    for i in range(20):
        (tmp_path / f"m{i}.py").write_text(f"def f{i}():\n    pass\n", encoding="utf-8")

    files = canary.sample_files(tmp_path, KrodoIgnore(tmp_path), sample_size=5)

    assert len(files) == 5


def test_sample_files_respects_ignore(tmp_path: Path) -> None:
    (tmp_path / "keep.py").write_text("def keep():\n    pass\n", encoding="utf-8")
    venv = tmp_path / ".venv"
    venv.mkdir()
    (venv / "vendored.py").write_text("def vendored():\n    pass\n", encoding="utf-8")

    files = canary.sample_files(tmp_path, KrodoIgnore(tmp_path))

    names = {p.name for p in files}
    assert names == {"keep.py"}


def test_sample_files_empty_workspace(tmp_path: Path) -> None:
    assert canary.sample_files(tmp_path, KrodoIgnore(tmp_path)) == []


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------


def test_probe_empty_sample_is_success(tmp_path: Path) -> None:
    ok, detail = canary.probe(tmp_path, KrodoIgnore(tmp_path))
    assert ok
    assert detail is None


def test_probe_success(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def a():\n    pass\n", encoding="utf-8")

    with patch("krodo.indexer.canary.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        ok, detail = canary.probe(tmp_path, KrodoIgnore(tmp_path))

    assert ok
    assert detail is None
    mock_run.assert_called_once()


def test_probe_detects_native_crash(tmp_path: Path) -> None:
    """A non-zero exit (e.g. -11 for SIGSEGV) is treated as a failed probe."""
    (tmp_path / "a.py").write_text("def a():\n    pass\n", encoding="utf-8")

    with patch("krodo.indexer.canary.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=-11)
        ok, detail = canary.probe(tmp_path, KrodoIgnore(tmp_path))

    assert not ok
    assert detail is not None
    assert "-11" in detail


def test_probe_detects_timeout(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def a():\n    pass\n", encoding="utf-8")

    with patch("krodo.indexer.canary.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="x", timeout=15.0)
        ok, detail = canary.probe(tmp_path, KrodoIgnore(tmp_path), timeout=15.0)

    assert not ok
    assert detail is not None
    assert "timed out" in detail


def test_probe_passes_sampled_files_to_subprocess(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def a():\n    pass\n", encoding="utf-8")

    with patch("krodo.indexer.canary.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        canary.probe(tmp_path, KrodoIgnore(tmp_path))

    argv = mock_run.call_args.args[0]
    assert argv[0:3] == [canary.sys.executable, "-m", "krodo.indexer.canary"]
    assert str(tmp_path / "a.py") in argv


# ---------------------------------------------------------------------------
# _main (subprocess entry point) — exercised in-process for coverage; the
# real subprocess boundary is covered by the mocked `probe` tests above.
# ---------------------------------------------------------------------------


def test_main_parses_valid_files(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text("def a():\n    pass\n", encoding="utf-8")

    assert canary._main([str(f)]) == 0


def test_main_skips_unreadable_paths(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.py"

    assert canary._main([str(missing)]) == 0


def test_main_with_no_args(tmp_path: Path) -> None:  # noqa: ARG001
    assert canary._main([]) == 0
