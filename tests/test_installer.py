"""Tests for cc-star installer."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cc_star.config import ConfigManager
from cc_star.installer import (
    HOOK_EVENTS,
    TemplateRenderer,
    _build_hook_config,
    _find_settings_target,
    _get_template_vars,
    _merge_hooks,
)


class TestTemplateRenderer:
    """Test template rendering."""

    def test_render_basic(self, tmp_template_dir):
        """Test basic template substitution."""
        renderer = TemplateRenderer(tmp_template_dir)
        vars = {
            "agent_name": "test-agent",
            "cache_path": "/tmp/.cc-star/data/cache.db",
            "ov_url": "http://localhost:1933",
            "ov_enabled": "True",
            "max_inject": "5",
            "tags": '["claude-code"]',
            "data_dir": "/tmp/.cc-star/data",
            "sessions_file": "/tmp/.cc-star/data/sessions.jsonl",
            "compact_state_file": "/tmp/.cc-star/data/compact_state.json",
            "memory_path": "",
            "status_path": "",
            "snapshot_path": "",
            "sync_batch": "50",
        }
        result = renderer.render("test_hook.py", vars)
        assert "test-agent" in result
        assert "/tmp/.cc-star/data/cache.db" in result

    def test_render_all_vars_substituted(self, tmp_template_dir):
        """Test that all template variables are substituted (no remaining $var)."""
        renderer = TemplateRenderer(tmp_template_dir)
        vars = {
            "agent_name": "agent",
            "cache_path": "/c.db",
            "ov_url": "",
            "ov_enabled": "False",
            "max_inject": "3",
            "tags": '["cc-star"]',
            "data_dir": "/d",
            "sessions_file": "/s.jsonl",
            "compact_state_file": "/c.json",
            "memory_path": "",
            "status_path": "",
            "snapshot_path": "",
            "sync_batch": "50",
        }
        result = renderer.render("test_hook.py", vars)
        # No remaining unsubstituted vars (like $agent_name, $ov_url, etc.)
        for var_name in vars:
            assert f"${{{var_name}}}" not in result, f"Variable {var_name} not substituted"

    def test_template_not_found(self, tmp_template_dir):
        """Test error on missing template."""
        renderer = TemplateRenderer(tmp_template_dir)
        with pytest.raises(FileNotFoundError):
            renderer.render("nonexistent.py", {})

    def test_render_with_special_values(self, tmp_template_dir):
        """Test rendering with empty/special values."""
        renderer = TemplateRenderer(tmp_template_dir)
        vars = {
            "agent_name": "",
            "cache_path": "",
            "ov_url": "",
            "ov_enabled": "False",
            "max_inject": "0",
            "tags": "[]",
            "data_dir": "",
            "sessions_file": "",
            "compact_state_file": "",
            "memory_path": "",
            "status_path": "",
            "snapshot_path": "",
            "sync_batch": "0",
        }
        # Should not crash with empty values
        result = renderer.render("test_hook.py", vars)
        assert isinstance(result, str)


class TestHookConfig:
    """Test hook config building."""

    def test_build_hook_config(self, tmp_path):
        """Test building hook config produces correct structure."""
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        config = {
            "hooks": {
                "timeout_inject": 10,
                "timeout_store": 15,
                "timeout_summary": 30,
                "timeout_session_start": 10,
                "timeout_compact_save": 5,
                "timeout_compact_restore": 10,
            },
        }

        result = _build_hook_config(hooks_dir, config)

        assert "SessionStart" in result
        assert "UserPromptSubmit" in result
        assert "Stop" in result
        assert "SessionEnd" in result
        assert "PreCompact" in result
        assert "PostCompact" in result

        for event, hooks in result.items():
            assert len(hooks) == 1
            assert hooks[0]["type"] == "command"
            assert "python" in hooks[0]["command"]
            assert isinstance(hooks[0]["timeout"], int)

    def test_pre_post_compact_have_args(self, tmp_path):
        """Test PreCompact/PostCompact hooks include save/restore args."""
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        config = {"hooks": {}}

        result = _build_hook_config(hooks_dir, config)

        assert "save" in result["PreCompact"][0]["command"]
        assert "restore" in result["PostCompact"][0]["command"]


class TestMergeHooks:
    """Test hook merging logic."""

    def test_merge_empty_existing(self):
        """Test merging into empty existing."""
        existing = {}
        new_hooks = {
            "UserPromptSubmit": [{"type": "command", "command": "python inject.py", "timeout": 10}],
        }
        result = _merge_hooks(existing, new_hooks)
        assert "hooks" in result
        assert "UserPromptSubmit" in result["hooks"]

    def test_merge_preserves_existing(self):
        """Test merging preserves existing unrelated hooks."""
        existing = {
            "hooks": {
                "Stop": [
                    {"type": "command", "command": "python custom.py", "timeout": 5},
                ],
            },
        }
        new_hooks = {
            "UserPromptSubmit": [{"type": "command", "command": "python inject.py", "timeout": 10}],
        }
        result = _merge_hooks(existing, new_hooks)
        assert "Stop" in result["hooks"]
        assert "UserPromptSubmit" in result["hooks"]
        assert len(result["hooks"]["Stop"]) == 1

    def test_merge_avoids_duplicates(self):
        """Test merging avoids duplicate entries for same event."""
        existing = {
            "hooks": {
                "UserPromptSubmit": [
                    {"type": "command", "command": "python inject.py", "timeout": 10},
                ],
            },
        }
        new_hooks = {
            "UserPromptSubmit": [{"type": "command", "command": "python inject.py", "timeout": 10}],
        }
        result = _merge_hooks(existing, new_hooks)
        assert len(result["hooks"]["UserPromptSubmit"]) == 1


class TestFindSettings:
    """Test settings file discovery."""

    def test_find_creates_local_when_no_settings(self, tmp_path):
        """Test that settings.local.json is created when no settings exist."""
        with (
            patch("cc_star.installer.Path.cwd", return_value=tmp_path),
            patch("cc_star.installer.Path.home", return_value=tmp_path),
        ):
            settings_path, is_local = _find_settings_target()
            assert settings_path.name == "settings.local.json"
            assert is_local

    def test_find_main_settings_with_hooks(self, tmp_path):
        """Test finding settings.json with existing hooks."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(parents=True)
        settings_file = claude_dir / "settings.json"
        settings_file.write_text(json.dumps({"hooks": {"Stop": []}}), encoding="utf-8")

        with (
            patch("cc_star.installer.Path.home", return_value=tmp_path),
            patch("cc_star.installer.Path.cwd", return_value=tmp_path),
        ):
            settings_path, is_local = _find_settings_target()
            assert settings_path == settings_file
            assert not is_local

    def test_find_main_settings_without_hooks(self, tmp_path):
        """Test finding settings.json without hooks key."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(parents=True)
        settings_file = claude_dir / "settings.json"
        settings_file.write_text(json.dumps({"skey": "sval"}), encoding="utf-8")

        with (
            patch("cc_star.installer.Path.home", return_value=tmp_path),
            patch("cc_star.installer.Path.cwd", return_value=tmp_path),
        ):
            settings_path, is_local = _find_settings_target()
            assert settings_path == settings_file
            assert not is_local


class TestTemplateVars:
    """Test template variable generation."""

    def test_get_template_vars_defaults(self):
        """Test template vars with default config."""
        config = {
            "agent": {"name": "assistant", "tags": ["claude-code"]},
            "storage": {"path": "~/.cc-star/data"},
            "memory": {"max_inject": 5, "memory_path": "", "status_path": "", "snapshot_path": ""},
            "ov": {"enabled": False, "url": "", "sync_batch": 50},
            "hooks": {},
        }
        vars = _get_template_vars(config)
        assert vars["agent_name"] == "assistant"
        assert vars["tags"] == '["claude-code"]'
        assert vars["ov_enabled"] == "False"
        assert vars["max_inject"] == "5"
        assert vars["sync_batch"] == "50"

    def test_get_template_vars_ov_enabled(self):
        """Test template vars with OV enabled."""
        config = {
            "agent": {"name": "my-agent", "tags": ["claude-code", "custom"]},
            "storage": {"path": "/custom/path"},
            "memory": {"max_inject": 10, "memory_path": "", "status_path": "", "snapshot_path": ""},
            "ov": {"enabled": True, "url": "http://ov:1933", "sync_batch": 100},
            "hooks": {},
        }
        vars = _get_template_vars(config)
        assert vars["agent_name"] == "my-agent"
        assert vars["ov_enabled"] == "True"
        assert vars["ov_url"] == "http://ov:1933"
        assert vars["sync_batch"] == "100"
        # Should contain "custom" in tags JSON
        assert "custom" in vars["tags"]


@pytest.fixture
def tmp_template_dir(tmp_path):
    """Create a temporary template directory with a test hook template."""
    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    content = (
        '#!/usr/bin/env python3\n'
        'CACHE_PATH = "$cache_path"\n'
        'AGENT = "$agent_name"\n'
        'OV = "$ov_enabled"\n'
        'MAX = $max_inject\n'
        'TAGS = $tags\n'
        'BATCH = $sync_batch\n'
        'MEMORY = "$memory_path"\n'
    )
    (template_dir / "test_hook.py").write_text(content, encoding="utf-8")
    return template_dir
