"""Tests for KrodoIgnore — 4-tier path filtering (M4 PR1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from krodo.sandbox.ignore import KrodoIgnore, PathIgnoredError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_ignore(tmp: Path, *, gitignore: str = "", krodoignore: str = "") -> KrodoIgnore:
    """Create a KrodoIgnore rooted at *tmp* with optional ignore file content."""
    if gitignore:
        (tmp / ".gitignore").write_text(gitignore)
    if krodoignore:
        (tmp / ".krodoignore").write_text(krodoignore)
    return KrodoIgnore(tmp)


# ---------------------------------------------------------------------------
# Tier 1 — hardcoded defaults (always active)
# ---------------------------------------------------------------------------


class TestHardcodedDefaults:
    def test_env_file_ignored(self, tmp_path: Path) -> None:
        ig = KrodoIgnore(tmp_path)
        assert ig.is_ignored(tmp_path / ".env")

    def test_env_dot_local_ignored(self, tmp_path: Path) -> None:
        ig = KrodoIgnore(tmp_path)
        assert ig.is_ignored(tmp_path / ".env.local")

    def test_pem_ignored(self, tmp_path: Path) -> None:
        ig = KrodoIgnore(tmp_path)
        assert ig.is_ignored(tmp_path / "certs" / "server.pem")

    def test_key_file_ignored(self, tmp_path: Path) -> None:
        ig = KrodoIgnore(tmp_path)
        assert ig.is_ignored(tmp_path / "private.key")

    def test_id_rsa_ignored(self, tmp_path: Path) -> None:
        ig = KrodoIgnore(tmp_path)
        assert ig.is_ignored(tmp_path / "id_rsa")
        assert ig.is_ignored(tmp_path / "id_rsa.pub")

    def test_id_ed25519_ignored(self, tmp_path: Path) -> None:
        ig = KrodoIgnore(tmp_path)
        assert ig.is_ignored(tmp_path / "id_ed25519")

    def test_credentials_json_ignored(self, tmp_path: Path) -> None:
        ig = KrodoIgnore(tmp_path)
        assert ig.is_ignored(tmp_path / "credentials.json")

    def test_pyc_ignored(self, tmp_path: Path) -> None:
        ig = KrodoIgnore(tmp_path)
        assert ig.is_ignored(tmp_path / "app" / "module.pyc")

    def test_so_ignored(self, tmp_path: Path) -> None:
        ig = KrodoIgnore(tmp_path)
        assert ig.is_ignored(tmp_path / "lib" / "_ext.so")

    def test_zip_ignored(self, tmp_path: Path) -> None:
        ig = KrodoIgnore(tmp_path)
        assert ig.is_ignored(tmp_path / "archive.zip")

    def test_node_modules_ignored(self, tmp_path: Path) -> None:
        ig = KrodoIgnore(tmp_path)
        assert ig.is_ignored(tmp_path / "node_modules" / "pkg" / "index.js")

    def test_pycache_ignored(self, tmp_path: Path) -> None:
        ig = KrodoIgnore(tmp_path)
        assert ig.is_ignored(tmp_path / "__pycache__" / "mod.cpython-312.pyc")

    def test_venv_ignored(self, tmp_path: Path) -> None:
        ig = KrodoIgnore(tmp_path)
        assert ig.is_ignored(tmp_path / ".venv" / "lib" / "python3.12")

    def test_normal_python_file_not_ignored(self, tmp_path: Path) -> None:
        ig = KrodoIgnore(tmp_path)
        assert not ig.is_ignored(tmp_path / "src" / "main.py")

    def test_readme_not_ignored(self, tmp_path: Path) -> None:
        ig = KrodoIgnore(tmp_path)
        assert not ig.is_ignored(tmp_path / "README.md")


# ---------------------------------------------------------------------------
# Tier 2 — .gitignore
# ---------------------------------------------------------------------------


class TestGitignore:
    def test_gitignore_pattern_respected(self, tmp_path: Path) -> None:
        ig = make_ignore(tmp_path, gitignore="*.log\n")
        assert ig.is_ignored(tmp_path / "app.log")

    def test_gitignore_dir_pattern(self, tmp_path: Path) -> None:
        ig = make_ignore(tmp_path, gitignore="dist/\n")
        assert ig.is_ignored(tmp_path / "dist" / "bundle.js")

    def test_file_not_in_gitignore_not_ignored(self, tmp_path: Path) -> None:
        ig = make_ignore(tmp_path, gitignore="*.log\n")
        assert not ig.is_ignored(tmp_path / "app.py")

    def test_missing_gitignore_ok(self, tmp_path: Path) -> None:
        ig = KrodoIgnore(tmp_path)  # no .gitignore
        assert not ig.is_ignored(tmp_path / "src" / "app.py")

    def test_gitignore_source_reported(self, tmp_path: Path) -> None:
        ig = make_ignore(tmp_path, gitignore="*.log\n")
        result = ig.match(tmp_path / "app.log")
        assert result.is_ignored
        assert result.source == ".gitignore"


# ---------------------------------------------------------------------------
# Tier 3 — project .krodoignore
# ---------------------------------------------------------------------------


class TestKrodoignore:
    def test_krodoignore_pattern_respected(self, tmp_path: Path) -> None:
        ig = make_ignore(tmp_path, krodoignore="secrets/\n")
        assert ig.is_ignored(tmp_path / "secrets" / "token.txt")

    def test_krodoignore_overrides_below_tiers(self, tmp_path: Path) -> None:
        # Not in .gitignore, but in .krodoignore
        ig = make_ignore(tmp_path, gitignore="*.log\n", krodoignore="config.yaml\n")
        assert ig.is_ignored(tmp_path / "config.yaml")
        assert ig.is_ignored(tmp_path / "app.log")  # gitignore still active

    def test_krodoignore_source_reported(self, tmp_path: Path) -> None:
        ig = make_ignore(tmp_path, krodoignore="internal/\n")
        result = ig.match(tmp_path / "internal" / "data.csv")
        assert result.is_ignored
        assert result.source == ".krodoignore"

    def test_missing_krodoignore_ok(self, tmp_path: Path) -> None:
        ig = KrodoIgnore(tmp_path)  # no .krodoignore
        assert not ig.is_ignored(tmp_path / "src" / "main.py")


# ---------------------------------------------------------------------------
# User-level override (Tier 4)
# ---------------------------------------------------------------------------


class TestUserLevelIgnore:
    def test_user_level_pattern_respected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_cfg = tmp_path / ".config" / "krodo"
        user_cfg.mkdir(parents=True)
        (user_cfg / "krodoignore").write_text("*.secret\n")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        ig = KrodoIgnore(tmp_path / "project")
        ig._user = KrodoIgnore._load_spec(user_cfg / "krodoignore")  # type: ignore[attr-defined]
        assert ig.is_ignored(tmp_path / "project" / "api.secret")

    def test_missing_user_config_ok(self, tmp_path: Path) -> None:
        ig = KrodoIgnore(tmp_path)  # user cfg doesn't exist
        assert not ig.is_ignored(tmp_path / "app.py")


# ---------------------------------------------------------------------------
# MatchResult and PathIgnoredError
# ---------------------------------------------------------------------------


class TestMatchResult:
    def test_not_ignored_result(self, tmp_path: Path) -> None:
        ig = KrodoIgnore(tmp_path)
        result = ig.match(tmp_path / "main.py")
        assert not result.is_ignored

    def test_ignored_result_has_source(self, tmp_path: Path) -> None:
        ig = KrodoIgnore(tmp_path)
        result = ig.match(tmp_path / ".env")
        assert result.is_ignored
        assert result.source == "hardcoded"

    def test_error_message_format(self, tmp_path: Path) -> None:
        ig = KrodoIgnore(tmp_path)
        result = ig.match(tmp_path / ".env")
        err = result.error()
        assert isinstance(err, PathIgnoredError)
        assert ".env" in str(err)
        assert "hardcoded" in str(err)

    def test_path_outside_workspace_not_ignored(self, tmp_path: Path) -> None:
        ig = KrodoIgnore(tmp_path / "project")
        # Path is outside workspace root — not matched (path firewall handles it)
        result = ig.match(Path("/etc/passwd"))
        assert not result.is_ignored


# ---------------------------------------------------------------------------
# Relative paths (tools often pass relative paths)
# ---------------------------------------------------------------------------


class TestRelativePaths:
    def test_relative_env_ignored(self, tmp_path: Path) -> None:
        ig = KrodoIgnore(tmp_path)
        assert ig.is_ignored(Path(".env"))

    def test_relative_src_not_ignored(self, tmp_path: Path) -> None:
        ig = KrodoIgnore(tmp_path)
        assert not ig.is_ignored(Path("src/main.py"))

    def test_relative_nested_env_ignored(self, tmp_path: Path) -> None:
        ig = KrodoIgnore(tmp_path)
        assert ig.is_ignored(Path("config/.env.production"))


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_file_content_ok(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text("")
        ig = KrodoIgnore(tmp_path)
        assert not ig.is_ignored(tmp_path / "app.py")

    def test_comment_lines_in_krodoignore(self, tmp_path: Path) -> None:
        (tmp_path / ".krodoignore").write_text("# This is a comment\n*.log\n")
        ig = KrodoIgnore(tmp_path)
        assert ig.is_ignored(tmp_path / "debug.log")
        assert not ig.is_ignored(tmp_path / "main.py")

    def test_is_ignored_convenience(self, tmp_path: Path) -> None:
        ig = KrodoIgnore(tmp_path)
        assert ig.is_ignored(tmp_path / ".env") is True
        assert ig.is_ignored(tmp_path / "main.py") is False

    def test_from_workspace(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock

        ws = MagicMock()
        ws.root = tmp_path
        ig = KrodoIgnore.from_workspace(ws)
        assert ig.is_ignored(tmp_path / ".env")

    def test_pathignorederror_attributes(self, tmp_path: Path) -> None:
        err = PathIgnoredError(Path(".env"), ".env", "hardcoded")
        assert err.path == Path(".env")
        assert err.pattern == ".env"
        assert err.source == "hardcoded"
