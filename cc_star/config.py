"""YAML-based configuration management for cc-star.

Config file lives at ~/.cc-star/config.yaml.
Uses a merge strategy so that adding new keys to defaults
does not break existing user configs.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG: dict[str, Any] = {
    "agent": {
        "name": "assistant",
        "tags": ["claude-code"],
    },
    "storage": {
        "path": "~/.cc-star/data",
    },
    "memory": {
        "max_inject": 5,
        "memory_path": "",
        "codex_extensions_path": "~/.codex/memories/extensions/cc-star",
        "status_path": "",
        "snapshot_path": "",
        "promote_enabled": True,
        "promote_threshold": 3,
        "promote_min_length": 150,
        "promote_cooldown_days": 7,
        "max_cache_mb": 1000,
        "max_inject_native": 3,
    },
    "ov": {
        "enabled": False,
        "url": "",
        "sync_batch": 50,
    },
    "hooks": {
        "timeout_inject": 10,
        "timeout_store": 15,
        "timeout_summary": 30,
        "timeout_session_start": 10,
        "timeout_compact_save": 5,
        "timeout_compact_restore": 10,
    },
}


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Deep-merge overlay into base (base is mutated and returned)."""
    for key, value in overlay.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


class ConfigManager:
    """Load, save, and access cc-star configuration."""

    def __init__(self, config_dir: str | None = None):
        self._config_dir = Path(
            os.path.expanduser(config_dir or "~/.cc-star")
        )
        self._config_file = self._config_dir / "config.yaml"
        self._data: dict[str, Any] | None = None

    @property
    def config_path(self) -> Path:
        return self._config_file

    @property
    def data_dir(self) -> Path:
        raw = self.get("storage.path")
        return Path(os.path.expanduser(raw))

    def load(self) -> dict[str, Any]:
        """Load config from YAML file, merged with defaults."""
        config = dict(DEFAULT_CONFIG)  # shallow copy top-level

        if self._config_file.is_file():
            try:
                raw = self._config_file.read_text(encoding="utf-8")
                user_config = yaml.safe_load(raw) or {}
                _deep_merge(config, user_config)
            except (yaml.YAMLError, OSError) as exc:
                # Backup corrupt file
                backup = self._config_file.with_suffix(".yaml.bak")
                try:
                    import shutil
                    shutil.copy2(str(self._config_file), str(backup))
                except OSError:
                    pass
                # Fall through with defaults

        self._data = config
        return config

    def save(self, config: dict[str, Any]) -> None:
        """Atomically write config to YAML file."""
        self._config_dir.mkdir(parents=True, exist_ok=True)

        fd, tmp_path = tempfile.mkstemp(
            suffix=".yaml",
            dir=str(self._config_dir),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                yaml.safe_dump(config, f, default_flow_style=False, allow_unicode=True)
            os.replace(tmp_path, str(self._config_file))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        self._data = config

    def get(self, key_path: str) -> Any:
        """Get a config value by dotted key path (e.g. 'agent.name')."""
        if self._data is None:
            self.load()
        keys = key_path.split(".")
        val: Any = self._data
        for k in keys:
            if isinstance(val, dict):
                val = val.get(k)
            else:
                return None
        return val

    def set(self, key_path: str, value: Any) -> dict[str, Any]:
        """Set a config value by dotted key path. Returns the updated config dict."""
        if self._data is None:
            self.load()

        config = self._data
        keys = key_path.split(".")
        for k in keys[:-1]:
            if k not in config or not isinstance(config[k], dict):
                config[k] = {}
            config = config[k]
        config[keys[-1]] = value

        self.save(self._data)
        return self._data
