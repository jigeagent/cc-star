#!/usr/bin/env python3
"""cc-star CLI — init, status, search, config subcommands."""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from pathlib import Path

# Reconfigure stdout for Windows terminals — use UTF-8, replace, don't crash
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer,
        encoding="utf-8",
        errors="replace",
        line_buffering=True,
    )

from cc_star.cache.connection import CacheConnection
from cc_star.cache.schema import ensure_schema
from cc_star.cache.traces import TraceRepository

from cc_star import __version__
from cc_star.config import ConfigManager
from cc_star.installer import HookInstaller


def _get_config_manager() -> ConfigManager:
    """Create a ConfigManager from the default config dir."""
    return ConfigManager()


def cmd_init(args: argparse.Namespace) -> None:
    """Initialize cc-star memory system."""
    cfg_mgr = _get_config_manager()
    config_dir = cfg_mgr.config_path.parent

    if config_dir.is_dir() and not args.force:
        if not args.non_interactive:
            print(f"cc-star already initialized at {config_dir}")
            print("Use --force to reinitialize")
        else:
            print(f"Already initialized (use --force to redo)")
        sys.exit(0)

    installer = HookInstaller(cfg_mgr)
    result = installer.install(
        agent_name=args.agent_name,
        ov_url=args.ov_url or "",
        non_interactive=args.non_interactive,
        force=args.force,
    )

    print(f"cc-star v{__version__} initialized successfully!")
    print(f"  Config:  {result['config_dir']}/config.yaml")
    print(f"  Data:    {result['cache_path']}")
    print(f"  Hooks:   {result['hooks_dir']}")
    print(f"  Agent:   {result['agent_name']}")
    print(f"  OV:      {'enabled (' + result['ov_url'] + ')' if result['ov_enabled'] else 'disabled'}")
    print()
    print("Next steps:")
    print("  1. Start a new Claude Code session")
    print("  2. Run 'cc-star status' to verify")
    print("  3. Run 'cc-star search \"your query\"' to test")


def cmd_status(args: argparse.Namespace) -> None:
    """Show memory system status."""
    cfg_mgr = _get_config_manager()
    config = cfg_mgr.load()
    data_dir = cfg_mgr.data_dir
    cache_path = data_dir / "cache.db"

    if not cache_path.is_file():
        print(f"cc-star not initialized. Run 'cc-star init' first.")
        sys.exit(1)

    # Open cache and get stats
    try:
        cache = CacheConnection(str(cache_path))
        ensure_schema(cache)
        repo = TraceRepository(cache)

        total = repo.count()
        unsynced = len(repo.get_unsynced(limit=999999))
        recent = repo.list_recent(limit=1)

        # DB file size
        db_size = cache_path.stat().st_size
        if db_size < 1024:
            size_str = f"{db_size} B"
        elif db_size < 1024 * 1024:
            size_str = f"{db_size / 1024:.1f} KB"
        else:
            size_str = f"{db_size / (1024 * 1024):.1f} MB"

        print(f"cc-star v{__version__}  running")
        print()
        print("Storage:")
        print(f"  Database: {cache_path} ({size_str})")
        print(f"  Traces:   {total}")
        print(f"  Unsynced: {unsynced}")

        # OV status
        ov_url = config.get("ov", {}).get("url", "")
        ov_enabled = config.get("ov", {}).get("enabled", False)
        if ov_enabled and ov_url:
            ov_ok = _check_ov_health(ov_url)
            if ov_ok:
                print(f"  OpenViking: configured  online")
            else:
                print(f"  OpenViking: configured  offline")
        else:
            print(f"  OpenViking: disabled")

        # Last session
        sessions_file = data_dir / "sessions.jsonl"
        if sessions_file.is_file():
            try:
                lines = sessions_file.read_text(encoding="utf-8").strip().split("\n")
                if lines and lines[0]:
                    last = json.loads(lines[-1])
                    prompt = last.get("first_prompt", "")
                    turns = last.get("turn_count", 0)
                    ts = last.get("timestamp", "")
                    if prompt:
                        print(f"  Last session: {turns} turns | \"{prompt[:60]}\" | {ts[:16]}")
            except (OSError, json.JSONDecodeError):
                pass

        cache.close_all()

    except Exception as e:
        print(f"Error reading cache: {e}")
        sys.exit(1)


def _safe(text: str, maxlen: int = 100) -> str:
    """Truncate and normalize whitespace for console output."""
    if not text:
        return ""
    return text.replace("\n", " ")[:maxlen]


def cmd_search(args: argparse.Namespace) -> None:
    """Search local memory."""
    cfg_mgr = _get_config_manager()
    data_dir = cfg_mgr.data_dir
    cache_path = data_dir / "cache.db"

    if not cache_path.is_file():
        print("cc-star not initialized. Run 'cc-star init' first.")
        sys.exit(1)

    try:
        cache = CacheConnection(str(cache_path))
        ensure_schema(cache)
        repo = TraceRepository(cache)

        results = repo.search_fts(args.query, limit=args.limit)

        if not results:
            print("No matches found.")
            sys.exit(0)

        print(f"Found {len(results)} matching memories:")
        print()

        for i, t in enumerate(results, 1):
            ts = (t.created_at or "")[:10]
            user_preview = _safe(t.user_content, 100)
            assistant_preview = _safe(t.assistant_content, 100)

            print(f"{i}. [{ts}] user: {user_preview}")
            if assistant_preview:
                print(f"   assistant: {assistant_preview}")
            print()

        cache.close_all()

    except Exception as e:
        print(f"Search error: {e}")
        sys.exit(1)


def cmd_config(args: argparse.Namespace) -> None:
    """Get/set configuration."""
    cfg_mgr = _get_config_manager()

    if not args.key:
        # Print all config
        config = cfg_mgr.load()
        import yaml
        yaml.safe_dump(config, sys.stdout, default_flow_style=False, allow_unicode=True)
        return

    if not args.value:
        # Get single key
        val = cfg_mgr.get(args.key)
        if val is None:
            print(f"Unknown key: {args.key}")
            sys.exit(1)
        print(val)
        return

    # Set key
    # Try to parse as JSON for complex values, otherwise use string
    try:
        parsed = json.loads(args.value)
    except (json.JSONDecodeError, ValueError):
        parsed = args.value

    cfg_mgr.set(args.key, parsed)
    print(f"Set {args.key} = {parsed}")

    # If OV settings changed, re-render hooks
    if args.key.startswith("ov.") or args.key.startswith("agent."):
        installer = HookInstaller(cfg_mgr)
        config = cfg_mgr.load()
        hooks_dir = cfg_mgr.config_path.parent / "hooks"
        installer._register_hooks(hooks_dir, config)
        print("Hooks re-registered with new config.")


def cmd_uninstall(args: argparse.Namespace) -> None:
    """Remove cc-star hooks from Claude Code settings."""
    cfg_mgr = _get_config_manager()
    installer = HookInstaller(cfg_mgr)
    if installer.uninstall():
        print("cc-star hooks removed from Claude Code settings.")
        print("To fully uninstall, also remove ~/.cc-star/ directory.")
    else:
        print("No cc-star hooks found in settings.")


def _check_ov_health(url: str) -> bool:
    """Check OpenViking connectivity."""
    if not url:
        return False
    try:
        import httpx
        r = httpx.get(f"{url}/health", timeout=2.0)
        return r.status_code == 200
    except Exception:
        return False


def main() -> None:
    """Entry point for cc-star CLI."""
    parser = argparse.ArgumentParser(
        prog="cc-star",
        description="Claude Code memory upgrade kit",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # init
    init_p = sub.add_parser("init", help="Initialize cc-star memory system")
    init_p.add_argument("--agent-name", default="assistant",
                        help="Agent name for tags and OV URIs")
    init_p.add_argument("--ov-url", default="",
                        help="OpenViking server URL (optional)")
    init_p.add_argument("--non-interactive", action="store_true",
                        help="Skip prompts, use defaults")
    init_p.add_argument("--force", action="store_true",
                        help="Reinitialize even if already configured")

    # status
    sub.add_parser("status", help="Show memory system status")

    # search
    search_p = sub.add_parser("search", help="Search local memory")
    search_p.add_argument("query", help="Search query")
    search_p.add_argument("--limit", type=int, default=8,
                          help="Max results (default: 8)")

    # config
    config_p = sub.add_parser("config", help="Get/set configuration")
    config_p.add_argument("key", nargs="?", help="Config key (e.g. agent.name)")
    config_p.add_argument("value", nargs="?", help="Config value")

    # uninstall
    sub.add_parser("uninstall", help="Remove cc-star hooks from Claude Code settings")

    args = parser.parse_args()

    # Dispatch
    if args.command == "init":
        cmd_init(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "search":
        cmd_search(args)
    elif args.command == "config":
        cmd_config(args)
    elif args.command == "uninstall":
        cmd_uninstall(args)


if __name__ == "__main__":
    main()
