"""Unit tests for src/krodo/core/config.py — config file loading (M5.4)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from krodo.core.config import (
    KrodoConfig,
    load_config,
    resolve_repo_map,
    resolve_repo_map_tokens,
    resolve_symbol_backend,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_toml(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Workspace YAML
# ---------------------------------------------------------------------------


class TestWorkspaceYaml:
    def test_loads_workspace_yaml(self, tmp_path: Path) -> None:
        """Workspace .krodo/config.yaml sets max_tokens."""
        _write_yaml(tmp_path / ".krodo" / "config.yaml", "max_tokens: 8000\n")

        with patch("pathlib.Path.home", return_value=tmp_path / "no-home"):
            cfg, sources = load_config(tmp_path)

        assert cfg.max_tokens == 8000
        assert any("config.yaml" in s for s in sources)

    def test_workspace_yaml_sets_model(self, tmp_path: Path) -> None:
        _write_yaml(
            tmp_path / ".krodo" / "config.yaml",
            "model: openai/gpt-4o\n",
        )
        with patch("pathlib.Path.home", return_value=tmp_path / "no-home"):
            cfg, _ = load_config(tmp_path)
        assert cfg.model == "openai/gpt-4o"

    def test_workspace_yaml_sets_approval(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path / ".krodo" / "config.yaml", "approval: full_auto\n")
        with patch("pathlib.Path.home", return_value=tmp_path / "no-home"):
            cfg, _ = load_config(tmp_path)
        assert cfg.approval == "full_auto"


# ---------------------------------------------------------------------------
# 2. User TOML merged under workspace YAML
# ---------------------------------------------------------------------------


class TestUserToml:
    def test_user_toml_merged_under_workspace_yaml(self, tmp_path: Path) -> None:
        """Workspace YAML wins over user TOML for the same key."""
        home_dir = tmp_path / "fake-home"
        _write_toml(
            home_dir / ".config" / "krodo" / "config.toml",
            'max_tokens = 4096\nmodel = "user-model"\n',
        )
        _write_yaml(tmp_path / ".krodo" / "config.yaml", "max_tokens: 8000\n")

        with patch("pathlib.Path.home", return_value=home_dir):
            cfg, sources = load_config(tmp_path)

        # workspace wins for max_tokens
        assert cfg.max_tokens == 8000
        # user toml contributes model (not overridden by workspace yaml)
        assert cfg.model == "user-model"
        assert len(sources) == 2

    def test_user_toml_only(self, tmp_path: Path) -> None:
        """User TOML is used when no workspace config exists."""
        home_dir = tmp_path / "fake-home"
        _write_toml(
            home_dir / ".config" / "krodo" / "config.toml",
            'model = "toml-model"\n',
        )
        with patch("pathlib.Path.home", return_value=home_dir):
            cfg, sources = load_config(tmp_path)

        assert cfg.model == "toml-model"
        assert len(sources) == 1
        assert "config.toml" in sources[0]


# ---------------------------------------------------------------------------
# 3. Missing files → empty config
# ---------------------------------------------------------------------------


class TestMissingFiles:
    def test_missing_files_returns_empty_config(self, tmp_path: Path) -> None:
        """No config files → all fields None, sources empty."""
        with patch("pathlib.Path.home", return_value=tmp_path / "no-home"):
            cfg, sources = load_config(tmp_path)

        assert cfg == KrodoConfig()
        assert sources == []

    def test_empty_yaml_returns_empty_config(self, tmp_path: Path) -> None:
        """Empty YAML file → empty config (no error)."""
        _write_yaml(tmp_path / ".krodo" / "config.yaml", "")
        with patch("pathlib.Path.home", return_value=tmp_path / "no-home"):
            cfg, _ = load_config(tmp_path)
        assert cfg.model is None


# ---------------------------------------------------------------------------
# 4. Schema error → warns, returns empty config
# ---------------------------------------------------------------------------


class TestSchemaError:
    def test_schema_error_warns_not_raises(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Invalid YAML value logs a warning and returns empty config."""
        # approval must be one of the ApprovalMode literals
        _write_yaml(tmp_path / ".krodo" / "config.yaml", "approval: invalid_value\n")

        import logging  # noqa: PLC0415

        with patch("pathlib.Path.home", return_value=tmp_path / "no-home"):
            with caplog.at_level(logging.WARNING, logger="krodo.core.config"):
                cfg, _ = load_config(tmp_path)

        assert cfg.approval is None  # schema error → field not set
        assert any("Config schema error" in r.message for r in caplog.records)

    def test_bad_yaml_syntax_warns_not_raises(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Syntactically invalid YAML logs a warning and returns empty config."""
        _write_yaml(tmp_path / ".krodo" / "config.yaml", ":::not yaml:::\n")
        import logging  # noqa: PLC0415

        with patch("pathlib.Path.home", return_value=tmp_path / "no-home"):
            with caplog.at_level(logging.WARNING, logger="krodo.core.config"):
                cfg, _ = load_config(tmp_path)

        assert cfg == KrodoConfig()


# ---------------------------------------------------------------------------
# 5. Sources list
# ---------------------------------------------------------------------------


class TestSources:
    def test_sources_include_both_paths_when_both_present(self, tmp_path: Path) -> None:
        home_dir = tmp_path / "fake-home"
        _write_toml(home_dir / ".config" / "krodo" / "config.toml", "max_tokens = 1000\n")
        _write_yaml(tmp_path / ".krodo" / "config.yaml", "max_tokens: 2000\n")

        with patch("pathlib.Path.home", return_value=home_dir):
            _, sources = load_config(tmp_path)

        assert len(sources) == 2
        # user TOML is lower priority → listed first
        assert "config.toml" in sources[0]
        assert "config.yaml" in sources[1]


# ---------------------------------------------------------------------------
# 7. symbol_backend validation matrix (M9)
# ---------------------------------------------------------------------------


class TestSymbolBackend:
    """Cover the str | list[str] | None schema and the review-C matrix."""

    def test_none_is_default(self) -> None:
        cfg = KrodoConfig()
        assert cfg.symbol_backend is None

    @pytest.mark.parametrize("value", ["treesitter", ["treesitter"]])
    def test_treesitter_accepted(self, value: object) -> None:
        assert KrodoConfig(symbol_backend=value).symbol_backend == value  # type: ignore[arg-type]

    @pytest.mark.parametrize("value", ["off", ["off"]])
    def test_off_accepted(self, value: object) -> None:
        assert KrodoConfig(symbol_backend=value).symbol_backend == value  # type: ignore[arg-type]

    @pytest.mark.parametrize("value", ["lsp", ["lsp"], ["lsp", "treesitter"]])
    def test_lsp_friendly_error(self, value: object) -> None:
        with pytest.raises(ValidationError) as exc_info:
            KrodoConfig(symbol_backend=value)  # type: ignore[arg-type]
        assert "Phase 3" in str(exc_info.value)

    def test_empty_list_rejected(self) -> None:
        with pytest.raises(ValidationError):
            KrodoConfig(symbol_backend=[])

    def test_unknown_value_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            KrodoConfig(symbol_backend="rust-analyzer")
        assert "legal: treesitter, off" in str(exc_info.value)

    def test_cannot_combine_treesitter_and_off(self) -> None:
        with pytest.raises(ValidationError):
            KrodoConfig(symbol_backend=["treesitter", "off"])

    def test_wrong_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            KrodoConfig(symbol_backend=42)  # type: ignore[arg-type]


class TestResolveSymbolBackend:
    def test_none_defaults_to_treesitter(self) -> None:
        assert resolve_symbol_backend(None) == "treesitter"

    @pytest.mark.parametrize("value", ["treesitter", ["treesitter"]])
    def test_treesitter_forms(self, value: object) -> None:
        assert resolve_symbol_backend(value) == "treesitter"  # type: ignore[arg-type]

    @pytest.mark.parametrize("value", ["off", ["off"]])
    def test_off_forms(self, value: object) -> None:
        assert resolve_symbol_backend(value) == "off"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 8. repo_map / repo_map_tokens (M10)
# ---------------------------------------------------------------------------


class TestRepoMapConfig:
    def test_defaults_are_none(self) -> None:
        cfg = KrodoConfig()
        assert cfg.repo_map is None
        assert cfg.repo_map_tokens is None

    def test_repo_map_accepts_bool(self) -> None:
        assert KrodoConfig(repo_map=True).repo_map is True
        assert KrodoConfig(repo_map=False).repo_map is False

    def test_repo_map_tokens_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            KrodoConfig(repo_map_tokens=0)
        with pytest.raises(ValidationError):
            KrodoConfig(repo_map_tokens=-512)
        assert KrodoConfig(repo_map_tokens=1024).repo_map_tokens == 1024


class TestRepoMapResolvers:
    def test_repo_map_none_defaults_true(self) -> None:
        assert resolve_repo_map(None) is True

    def test_repo_map_passes_bool(self) -> None:
        assert resolve_repo_map(True) is True
        assert resolve_repo_map(False) is False

    def test_repo_map_tokens_none_defaults_2048(self) -> None:
        assert resolve_repo_map_tokens(None) == 2048

    def test_repo_map_tokens_passes_int(self) -> None:
        assert resolve_repo_map_tokens(1024) == 1024
