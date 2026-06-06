"""Tests for cc-star CLI commands."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cc_star import __version__
from cc_star.cli import cmd_config, cmd_init, cmd_search, cmd_status


class TestCmdConfig:
    """Test config subcommand."""

    def test_config_get_all(self, tmp_config_dir, capsys):
        """Test printing all config."""
        tmp_config_dir.mkdir(parents=True, exist_ok=True)
        cfg_path = tmp_config_dir / "config.yaml"
        cfg_path.write_text("agent:\n  name: test-agent\n", encoding="utf-8")

        with patch("cc_star.cli._get_config_manager") as mock_mgr:
            mgr = MagicMock()
            mgr.config_path.parent = tmp_config_dir
            mgr.load.return_value = {
                "agent": {"name": "test-agent", "tags": ["claude-code"]},
                "storage": {"path": str(tmp_config_dir / "data")},
                "memory": {"max_inject": 5},
                "ov": {"enabled": False, "url": "", "sync_batch": 50},
                "hooks": {},
            }
            mock_mgr.return_value = mgr

            args = MagicMock()
            args.key = None
            args.value = None
            cmd_config(args)
            captured = capsys.readouterr()
            assert "test-agent" in captured.out

    def test_config_get_key(self, tmp_config_dir, capsys):
        """Test getting a single config key."""
        with patch("cc_star.cli._get_config_manager") as mock_mgr:
            mgr = MagicMock()
            mgr.get.return_value = "assistant"
            mock_mgr.return_value = mgr

            args = MagicMock()
            args.key = "agent.name"
            args.value = None
            cmd_config(args)
            captured = capsys.readouterr()
            assert "assistant" in captured.out

    def test_config_set_key(self, tmp_config_dir, capsys):
        """Test setting a config key."""
        with patch("cc_star.cli._get_config_manager") as mock_mgr:
            mgr = MagicMock()
            mock_mgr.return_value = mgr

            # Also patch installer for hooks re-registration
            with patch("cc_star.cli.HookInstaller") as mock_installer:
                args = MagicMock()
                args.key = "agent.name"
                args.value = "new-agent"
                cmd_config(args)
                captured = capsys.readouterr()
                assert "new-agent" in captured.out
                mgr.set.assert_called_with("agent.name", "new-agent")

    def test_config_unknown_key(self, capsys):
        """Test getting an unknown key returns error."""
        with patch("cc_star.cli._get_config_manager") as mock_mgr:
            mgr = MagicMock()
            mgr.get.return_value = None
            mock_mgr.return_value = mgr

            args = MagicMock()
            args.key = "unknown.key"
            args.value = None
            with pytest.raises(SystemExit) as exc:
                cmd_config(args)
            assert exc.value.code == 1


class TestCmdInit:
    """Test init subcommand."""

    def test_init_already_initialized(self, tmp_config_dir, capsys):
        """Test init when already initialized without --force."""
        config_dir = tmp_config_dir
        config_dir.mkdir(parents=True, exist_ok=True)

        with patch("cc_star.cli._get_config_manager") as mock_mgr:
            mgr = MagicMock()
            mgr.config_path.parent = config_dir
            mock_mgr.return_value = mgr

            args = MagicMock()
            args.force = False
            args.non_interactive = True
            with pytest.raises(SystemExit) as exc:
                cmd_init(args)
            assert exc.value.code == 0  # exits gracefully
            captured = capsys.readouterr()
            # No assertion on exact message since it varies


class TestCmdSearch:
    """Test search subcommand."""

    def test_search_no_cache(self, tmp_config_dir, capsys):
        """Test search when cache.db doesn't exist."""
        with patch("cc_star.cli._get_config_manager") as mock_mgr:
            mgr = MagicMock()
            mgr.data_dir = tmp_config_dir
            mock_mgr.return_value = mgr

            args = MagicMock()
            args.query = "test"
            args.limit = 8
            with pytest.raises(SystemExit) as exc:
                cmd_search(args)
            assert exc.value.code == 1
            captured = capsys.readouterr()
            assert "not initialized" in captured.out.lower()


class TestCmdStatus:
    """Test status subcommand."""

    def test_status_no_cache(self, tmp_config_dir, capsys):
        """Test status when cache.db doesn't exist."""
        with patch("cc_star.cli._get_config_manager") as mock_mgr:
            mgr = MagicMock()
            mgr.data_dir = tmp_config_dir
            mgr.load.return_value = {
                "ov": {"enabled": False, "url": ""},
            }
            mock_mgr.return_value = mgr

            args = MagicMock()
            with pytest.raises(SystemExit) as exc:
                cmd_status(args)
            assert exc.value.code == 1
            captured = capsys.readouterr()
            assert "not initialized" in captured.out.lower()


@pytest.fixture
def tmp_config_dir(tmp_path):
    """Create a temporary config directory."""
    return tmp_path / ".cc-star"
