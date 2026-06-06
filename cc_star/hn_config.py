"""Configuration management for Hermes Next."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]


@dataclass
class OpenVikingConfig:
    """OpenViking server connection settings."""

    base_url: str = "http://localhost:1933"
    api_key: Optional[str] = None
    timeout: float = 30.0
    max_retries: int = 3


@dataclass
class CacheConfig:
    """Local SQLite cache settings."""

    path: str = "~/.hermes-next/cache.db"
    enable_fts: bool = True
    wal_mode: bool = True


@dataclass
class AgentConfig:
    """Agent identity settings."""

    name: str = "default"
    role: str = "assistant"


@dataclass
class RetrievalConfig:
    """Fusion retrieval pipeline settings."""

    semantic_k: int = 16
    fts_k: int = 8
    policy_k: int = 4
    timeline_k: int = 8
    rrf_k: int = 60
    mmr_lambda: float = 0.7
    mmr_k: int = 8
    recency_decay: float = 0.9


@dataclass
class HermesNextConfig:
    """Top-level configuration."""

    openviking: OpenVikingConfig = field(default_factory=OpenVikingConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)

    @classmethod
    def load(cls, path: Optional[str] = None) -> "HermesNextConfig":
        """Load config from YAML file, with env var overrides."""
        cfg = cls()

        # Try loading from file
        if path:
            cfg._load_file(path)
        else:
            # Search default locations
            for candidate in cls._default_paths():
                if candidate.exists():
                    cfg._load_file(str(candidate))
                    break

        # Env var overrides
        cfg._apply_env_overrides()

        return cfg

    def _load_file(self, path: str) -> None:
        """Load config from a YAML file."""
        p = Path(path).expanduser()
        if not p.exists():
            return
        raw = p.read_text(encoding="utf-8")
        if yaml is None:
            return
        data = yaml.safe_load(raw) or {}

        if "openviking" in data:
            ov = data["openviking"]
            if "base_url" in ov:
                self.openviking.base_url = ov["base_url"]
            if "api_key" in ov:
                self.openviking.api_key = ov.get("api_key")
            if "timeout" in ov:
                self.openviking.timeout = float(ov["timeout"])

        if "cache" in data:
            c = data["cache"]
            if "path" in c:
                self.cache.path = c["path"]
            if "enable_fts" in c:
                self.cache.enable_fts = bool(c["enable_fts"])

        if "agent" in data:
            a = data["agent"]
            if "name" in a:
                self.agent.name = a["name"]
            if "role" in a:
                self.agent.role = a["role"]

        if "retrieval" in data:
            r = data["retrieval"]
            for key in ("semantic_k", "fts_k", "policy_k", "timeline_k",
                        "rrf_k", "mmr_lambda", "mmr_k", "recency_decay"):
                if key in r:
                    setattr(self.retrieval, key, r[key])

    def _apply_env_overrides(self) -> None:
        """Apply environment variable overrides."""
        env_map = {
            "HERMES_NEXT_OV_URL": ("openviking", "base_url"),
            "HERMES_NEXT_OV_API_KEY": ("openviking", "api_key"),
            "HERMES_NEXT_CACHE_PATH": ("cache", "path"),
            "HERMES_NEXT_AGENT_NAME": ("agent", "name"),
        }
        for env_var, (section, attr) in env_map.items():
            val = os.environ.get(env_var)
            if val is not None:
                getattr(self, section).__setattr__(attr, val)

    @staticmethod
    def _default_paths() -> list[Path]:
        """Return list of default config file paths to search."""
        home = Path.home()
        return [
            home / ".config" / "hermes-next" / "hermes-next.yaml",
            home / ".hermes-next" / "hermes-next.yaml",
            home / ".hermes-next.yaml",
            Path.cwd() / "hermes-next.yaml",
            Path.cwd() / ".hermes-next.yaml",
        ]


def get_config(path: Optional[str] = None) -> HermesNextConfig:
    """Convenience function to load configuration."""
    return HermesNextConfig.load(path)
