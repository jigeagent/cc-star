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
        print(f"  ⚠️  cc-star 已初始化于 {config_dir}")
        print(f"  使用 --force 可重新初始化")
        print(f"  使用 cc-star doctor 可做运行检查")
        sys.exit(0)

    if config_dir.is_dir() and args.force:
        print(f"  🔄 重新初始化 cc-star ...")

    installer = HookInstaller(cfg_mgr)
    result = installer.install(
        agent_name=args.agent_name,
        ov_url=args.ov_url or "",
        non_interactive=args.non_interactive,
        force=args.force,
    )

    # ── 输出 ──
    print()
    print(f"  ✅ cc-star v{__version__}  初始化成功")
    print(f"  ───────────────────────────────────")
    print(f"  配置目录  {result['config_dir']}")
    print(f"  数据文件  {result['cache_path']}")
    print(f"  Hook 脚本 {result['hooks_dir']}")
    print(f"  当前身份  {result['agent_name']}")
    ov_status = f"已连接 {result['ov_url']}" if result['ov_enabled'] else "未配置"
    print(f"  OpenViking {ov_status}")
    print()

    # ── 自动检测 ──
    checks = []
    # Python version
    pyver = f"{sys.version_info.major}.{sys.version_info.minor}"
    checks.append(f"  ✓ Python {pyver}")

    # Config file
    if (config_dir / "config.yaml").is_file():
        checks.append(f"  ✓ 配置文件已写入")

    # Hooks in settings.json
    settings_path = Path.home() / ".claude" / "settings.json"
    if settings_path.is_file():
        try:
            import json
            s = json.loads(settings_path.read_text(encoding="utf-8"))
            hook_count = sum(len(v) for v in (s.get("hooks", {}) or {}).values())
            checks.append(f"  ✓ Claude Code hooks 已注册 ({hook_count} 事件)")
        except Exception:
            pass

    # OpenViking connectivity
    ov_url = result.get("ov_url", "")
    if ov_url:
        try:
            import httpx
            r = httpx.get(f"{ov_url}/health", timeout=2.0)
            if r.status_code == 200:
                checks.append(f"  ✓ OpenViking 在线 ({ov_url})")
            else:
                checks.append(f"  ⚠️ OpenViking 返回异常状态")
        except Exception:
            checks.append(f"  ⚠️ OpenViking 不可达 ({ov_url})")

    # Native memory
    mem_path = cfg_mgr.get("memory.memory_path")
    if mem_path:
        mem_dir = Path(os.path.expanduser(mem_path))
        if mem_dir.is_dir():
            count = len(list(mem_dir.glob("*.md")))
            checks.append(f"  ✓ 原生记忆目录就绪 ({count} 份文件)")

    # Cache DB
    cache_path = cfg_mgr.data_dir / "cache.db"
    if cache_path.is_file():
        size_mb = cache_path.stat().st_size / (1024 * 1024)
        checks.append(f"  ✓ 记忆数据库 {size_mb:.0f}MB ({result.get('trace_count', '?')} 条)")

    print(f"  ── 环境检测 ──")
    for c in checks:
        print(f"  {c}")
    print()

    # ── 下一步 ──
    print(f"  ── 下一步 ──")
    print(f"  1. 启动新的 Claude Code 会话（hooks 将在新会话生效）")
    print(f"  2. 运行 cc-star doctor    全面自检")
    print(f"  3. 运行 cc-star status    查看运行状态")
    print(f"  4. 运行 cc-star promote   记忆维护（建议每周一次）")
    print(f"  5. 运行 cc-star search    测试记忆检索")
    print()

    # ── 首次使用提示 ──
    print(f"  💡 cc-star v0.3 三核能力")
    print(f"     🔍 三源检索  对话 + 核心记忆 + 团队共享 → RRF 融合")
    print(f"     ⬆  自动晋升  高频内容自动写入原生记忆")
    print(f"     🧹 生命周期   自动回收 + 去重 + 热扫")
    print()


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


def cmd_promote(args: argparse.Namespace) -> None:
    """Run memory maintenance: cache limit, dedup, hot promote."""
    from cc_star.promote import run_maintenance
    results = run_maintenance(dry_run=args.dry_run or False)
    json.dump(results, sys.stdout, indent=2, ensure_ascii=False)


def cmd_doctor(args: argparse.Namespace) -> None:
    """全面自检：环境 + 配置 + hook + DB + OV 一次查清."""
    print()
    print(f"  🏥 cc-star doctor — 全面自检")
    print(f"  ───────────────────────────────────")
    print()

    cfg_mgr = _get_config_manager()
    config_dir = cfg_mgr.config_path.parent
    data_dir = cfg_mgr.data_dir
    all_ok = True

    # 1. 配置
    if (config_dir / "config.yaml").is_file():
        print(f"  ✅ 配置文件  {config_dir / 'config.yaml'}")
    else:
        print(f"  ❌ 配置文件缺失 — 请运行 cc-star init")
        all_ok = False

    # 2. 数据库
    cache_path = data_dir / "cache.db"
    if cache_path.is_file():
        size_mb = cache_path.stat().st_size / (1024 * 1024)
        import sqlite3
        try:
            conn = sqlite3.connect(str(cache_path))
            count = conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
            conn.close()
            print(f"  ✅ 记忆数据库  {cache_path} ({size_mb:.0f}MB, {count} 条)")
        except Exception as e:
            print(f"  ❌ 数据库异常 — {e}")
            all_ok = False
    else:
        print(f"  ⚠️ 数据库文件不存在（新装机正常，使用后会自动创建）")

    # 3. Hook 脚本
    hooks_dir = config_dir / "hooks"
    expected = ["session_start.py", "inject.py", "store.py", "summary.py", "compact.py"]
    if hooks_dir.is_dir():
        present = [p.name for p in hooks_dir.glob("*.py")]
        missing = [f for f in expected if f not in present]
        if not missing:
            print(f"  ✅ Hook 脚本  {len(present)}/5 齐全")
        else:
            print(f"  ⚠️ Hook 脚本缺失 — {missing}")
            all_ok = False
    else:
        print(f"  ❌ Hook 目录缺失 — 请运行 cc-star init --force")
        all_ok = False

    # 4. Claude Code settings hooks
    settings_path = Path.home() / ".claude" / "settings.json"
    if settings_path.is_file():
        try:
            import json
            s = json.loads(settings_path.read_text(encoding="utf-8"))
            hooks = s.get("hooks", {})
            cc_events = [e for e in hooks if hooks[e]]
            print(f"  ✅ Claude Code {len(cc_events)}/{len(hooks)} 事件已注册 hook")
        except Exception as e:
            print(f"  ⚠️ 读取 settings.json 失败 — {e}")
    else:
        print(f"  ⚠️ Claude Code settings.json 不存在（未安装 Claude Code?）")

    # 5. 原生记忆
    mem_path = cfg_mgr.get("memory.memory_path")
    if mem_path:
        mem_dir = Path(os.path.expanduser(mem_path))
        if mem_dir.is_dir():
            count = len(list(mem_dir.glob("*.md")))
            print(f"  ✅ 原生记忆  {mem_dir} ({count} 份文件)")
        else:
            print(f"  ⚠️ 原生记忆目录不存在 — 将自动创建")
    else:
        print(f"  ⚠️ 原生记忆未配置 — 设置 memory.memory_path 可启用")

    # 6. 快照 / STATUS
    for key, label in [("memory.status_path", "STATUS"), ("memory.snapshot_path", "快照")]:
        path = cfg_mgr.get(key)
        if path:
            p = Path(os.path.expanduser(path))
            if p.is_file():
                print(f"  ✅ {label}文件  {p}")
            elif p.exists():
                print(f"  ✅ {label}路径  {p}")
            else:
                print(f"  ⚠️ {label}路径不存在 — {p}")

    # 7. OpenViking
    ov_url = cfg_mgr.get("ov.url")
    ov_enabled = cfg_mgr.get("ov.enabled")
    if ov_enabled and ov_url:
        try:
            import httpx
            r = httpx.get(f"{ov_url}/health", timeout=2.0)
            if r.status_code == 200:
                print(f"  ✅ OpenViking 在线  {ov_url}")
            else:
                print(f"  ⚠️ OpenViking 异常状态 ({r.status_code})")
        except Exception:
            print(f"  ❌ OpenViking 不可达  {ov_url}")
            all_ok = False
    else:
        print(f"  ⚪ OpenViking 未配置（可选）")

    print()
    if all_ok:
        print(f"  ✅ 全部就绪！cc-star v{__version__} 运行正常")
    else:
        print(f"  ⚠️ 存在需要修复的项目，请按上述提示操作")
    print()


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

    # promote
    promote_p = sub.add_parser("promote", help="Run memory maintenance (cache limit, dedup, hot promote)")
    promote_p.add_argument("--dry-run", "-n", action="store_true",
                           help="Preview without making changes")

    # doctor
    sub.add_parser("doctor", help="全面自检：环境 + 配置 + hook + DB + OV 一次查清")

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
    elif args.command == "promote":
        cmd_promote(args)
    elif args.command == "doctor":
        cmd_doctor(args)


if __name__ == "__main__":
    main()
