"""Hook installer — template rendering + Claude Code hook registration."""

from __future__ import annotations

import json
import os
import shutil
import string
import sys
from pathlib import Path
from string import Template
from typing import Any

from cc_star.cache.connection import CacheConnection
from cc_star.cache.schema import ensure_schema

from cc_star.config import ConfigManager

HOOK_EVENTS: dict[str, dict[str, Any]] = {
    "SessionStart": {
        "template": "session_start.py",
        "timeout_key": "timeout_session_start",
    },
    "UserPromptSubmit": {
        "template": "inject.py",
        "timeout_key": "timeout_inject",
    },
    "Stop": {
        "template": "store.py",
        "timeout_key": "timeout_store",
    },
    "SessionEnd": {
        "template": "summary.py",
        "timeout_key": "timeout_summary",
    },
    "PreCompact": {
        "template": "compact.py",
        "timeout_key": "timeout_compact_save",
        "args": ["save"],
    },
    "PostCompact": {
        "template": "compact.py",
        "timeout_key": "timeout_compact_restore",
        "args": ["restore"],
    },
}


def _find_settings_target() -> tuple[Path, bool]:
    """Find the best settings file for hook registration.

    Returns: (settings_path, is_local) where is_local indicates settings.local.json.
    Strategy:
    1. If ~/.claude/settings.json exists and has no 'hooks' key -> write directly
    2. If it has 'hooks' key -> merge into it
    3. If .claude/settings.local.json exists relative to CWD -> merge into it
    4. Otherwise -> create .claude/settings.local.json relative to CWD
    """
    claude_dir = Path.home() / ".claude"
    settings_json = claude_dir / "settings.json"
    cwd_claude = Path.cwd() / ".claude"
    settings_local = cwd_claude / "settings.local.json"

    # Check main settings.json
    if settings_json.is_file():
        try:
            content = json.loads(settings_json.read_text(encoding="utf-8"))
            if content.get("hooks"):
                # Has hooks -> merge into it
                return settings_json, False
            else:
                # No hooks -> we can write directly
                return settings_json, False
        except (json.JSONDecodeError, OSError):
            pass

    # Check if settings.local.json exists in CWD's .claude
    if settings_local.is_file():
        return settings_local, True

    # Create settings.local.json in CWD's .claude
    cwd_claude.mkdir(parents=True, exist_ok=True)
    return settings_local, True


def _build_hook_config(
    hooks_dir: Path,
    config: dict[str, Any],
) -> dict[str, list[dict]]:
    """Build the hooks configuration dict for Claude Code settings."""
    hook_config: dict[str, list[dict]] = {}

    for event, info in HOOK_EVENTS.items():
        script_path = hooks_dir / info["template"]
        timeout = config.get("hooks", {}).get(info["timeout_key"], 10)

        cmd_parts = ["python", str(script_path)]
        if info.get("args"):
            cmd_parts.extend(info["args"])

        entry = {
            "type": "command",
            "command": " ".join(cmd_parts),
            "timeout": timeout,
        }

        hook_config[event] = [entry]

    return hook_config


def _merge_hooks(
    existing: dict[str, Any],
    new_hooks: dict[str, list[dict]],
) -> dict[str, Any]:
    """Merge new hooks into existing settings, avoiding duplicates by script path."""
    result = dict(existing)
    existing_hooks = result.get("hooks", {})

    for event, hooks in new_hooks.items():
        if event not in existing_hooks:
            existing_hooks[event] = hooks
        else:
            # Append hooks that don't already exist (check by script path)
            existing_scripts = set()
            for h in existing_hooks[event]:
                if isinstance(h, dict):
                    existing_scripts.add(h.get("command", ""))

            for h in hooks:
                if isinstance(h, dict) and h.get("command", "") not in existing_scripts:
                    existing_hooks[event].append(h)

    result["hooks"] = existing_hooks
    return result


def _get_template_vars(config: dict[str, Any]) -> dict[str, str]:
    """Build the template substitution dict from config."""
    storage_path = os.path.expanduser(config.get("storage", {}).get("path", "~/.cc-star/data"))
    ov_enabled = config.get("ov", {}).get("enabled", False)

    return {
        "agent_name": config.get("agent", {}).get("name", "assistant"),
        "tags": json.dumps(config.get("agent", {}).get("tags", ["claude-code"])),
        "data_dir": storage_path,
        "cache_path": os.path.join(storage_path, "cache.db"),
        "sessions_file": os.path.join(storage_path, "sessions.jsonl"),
        "compact_state_file": os.path.join(storage_path, "compact_state.json"),
        "ov_url": config.get("ov", {}).get("url", ""),
        "ov_enabled": "True" if ov_enabled else "False",
        "max_inject": str(config.get("memory", {}).get("max_inject", 5)),
        "memory_path": config.get("memory", {}).get("memory_path", ""),
        "status_path": config.get("memory", {}).get("status_path", ""),
        "snapshot_path": config.get("memory", {}).get("snapshot_path", ""),
        "sync_batch": str(config.get("ov", {}).get("sync_batch", 50)),
    }


class TemplateRenderer:
    """Renders hook templates using string.Template."""

    def __init__(self, template_dir: str | Path):
        self._template_dir = Path(template_dir)
        self._cache: dict[str, Template] = {}

    def render(self, template_name: str, vars: dict[str, str]) -> str:
        """Render a template with the given variables."""
        if template_name not in self._cache:
            tmpl_path = self._template_dir / template_name
            if not tmpl_path.is_file():
                raise FileNotFoundError(f"Template not found: {tmpl_path}")
            content = tmpl_path.read_text(encoding="utf-8")
            self._cache[template_name] = Template(content)

        return self._cache[template_name].substitute(vars)

    def list_templates(self) -> list[str]:
        """List all template files in the template directory."""
        return sorted(
            p.name for p in self._template_dir.iterdir()
            if p.is_file() and not p.name.startswith(".")
        )


class HookInstaller:
    """Installer for cc-star hook scripts."""

    def __init__(self, config_manager: ConfigManager):
        self._cfg_mgr = config_manager
        pkg_dir = Path(__file__).parent
        template_dir = pkg_dir / "templates"
        self._renderer = TemplateRenderer(template_dir)

    def install(self, agent_name: str, ov_url: str = "",
                non_interactive: bool = False, force: bool = False) -> dict[str, Any]:
        """Full initialization sequence. Returns status dict."""
        config_dir = self._cfg_mgr.config_path.parent
        data_dir = self._cfg_mgr.data_dir
        hooks_dir = config_dir / "hooks"

        # 1. Create directory structure
        data_dir.mkdir(parents=True, exist_ok=True)
        hooks_dir.mkdir(parents=True, exist_ok=True)

        # 2. Write config.yaml
        config = self._cfg_mgr.load()
        if agent_name:
            config["agent"]["name"] = agent_name
        if ov_url:
            config["ov"]["url"] = ov_url
            config["ov"]["enabled"] = True
        self._cfg_mgr.save(config)

        # 3. Initialize SQLite database
        cache_path = data_dir / "cache.db"
        cache = CacheConnection(str(cache_path))
        ensure_schema(cache)
        cache.close_all()

        # 4. Render and write hook scripts
        vars = _get_template_vars(config)
        for tmpl_name in self._renderer.list_templates():
            rendered = self._renderer.render(tmpl_name, vars)
            output_name = tmpl_name.replace(".j2", "")
            output_path = hooks_dir / output_name
            output_path.write_text(rendered, encoding="utf-8")
            # chmod +x on Unix
            if os.name != "nt":
                output_path.chmod(0o755)

        # 5. Write empty sessions.jsonl
        sessions_file = data_dir / "sessions.jsonl"
        if not sessions_file.is_file():
            sessions_file.write_text("", encoding="utf-8")

        # 6. Register hooks in Claude Code settings
        self._register_hooks(hooks_dir, config)

        return {
            "config_dir": str(config_dir),
            "data_dir": str(data_dir),
            "hooks_dir": str(hooks_dir),
            "cache_path": str(cache_path),
            "agent_name": config["agent"]["name"],
            "ov_enabled": config["ov"]["enabled"],
            "ov_url": config["ov"]["url"],
        }

    def uninstall(self) -> bool:
        """Remove cc-star hook entries from Claude Code settings."""
        settings_path, _ = _find_settings_target()
        if not settings_path.is_file():
            return False

        try:
            content = json.loads(settings_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False

        hooks = content.get("hooks", {})
        if not hooks:
            return False

        modified = False
        cc_star_events = set(HOOK_EVENTS.keys())

        for event in cc_star_events:
            if event in hooks:
                original = list(hooks[event])
                hooks[event] = [
                    h for h in hooks[event]
                    if not (isinstance(h, dict) and "cc-star" in h.get("command", ""))
                ]
                if len(hooks[event]) != len(original):
                    modified = True
                if not hooks[event]:
                    del hooks[event]

        if modified:
            content["hooks"] = hooks
            settings_path.write_text(
                json.dumps(content, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

        return modified

    def _register_hooks(self, hooks_dir: Path, config: dict[str, Any]) -> None:
        """Register hook scripts in Claude Code settings."""
        settings_path, is_local = _find_settings_target()
        hooks_config = _build_hook_config(hooks_dir, config)

        if settings_path.is_file():
            try:
                existing = json.loads(settings_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                existing = {}
        else:
            existing = {}

        merged = _merge_hooks(existing, hooks_config)
        settings_path.parent.mkdir(parents=True, exist_ok=True)

        # Atomic write
        tmp_path = settings_path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(merged, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        shutil.move(str(tmp_path), str(settings_path))
