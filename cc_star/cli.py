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
from cc_star.memos import patterns as pattern_capture


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

        # Embedding coverage
        embedded = repo.count_embedded()
        pct = (embedded / total * 100) if total > 0 else 0
        warn = " 嵌入覆盖率低于 50%" if pct < 50 else ""
        print(f"  嵌入覆盖率:  {embedded} / {total} ({pct:.0f}%){warn}")

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

        # hot.md status
        hot_file = data_dir / "hot.md"
        if hot_file.is_file():
            try:
                hot_size = hot_file.stat().st_size
                hot_age_hours = 0
                for line in hot_file.read_text(encoding="utf-8").split("\n"):
                    if line.startswith("updated_at:"):
                        from datetime import datetime, timezone
                        try:
                            up = datetime.fromisoformat(line.split(":", 1)[1].strip())
                            hot_age_hours = (datetime.now(timezone.utc) - up).total_seconds() / 3600
                        except (ValueError, TypeError):
                            pass
                        break
                age_hint = f" ({hot_age_hours:.0f}h 前)" if hot_age_hours > 0 else ""
                print(f"  hot.md:   {hot_size}b{age_hint}")
            except OSError:
                pass
        else:
            print(f"  hot.md:   (not written yet)")

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
    from cc_star.promote import run_maintenance, backfill_embeddings

    if args.backfill_embeddings:
        result = backfill_embeddings()
        json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
        return

    results = run_maintenance(dry_run=args.dry_run or False)
    json.dump(results, sys.stdout, indent=2, ensure_ascii=False)


def cmd_graph(args: argparse.Namespace) -> None:
    """Context graph operations."""
    from cc_star.graph.repository import GraphRepository

    data_dir = _get_config_manager().data_dir
    graph_path = data_dir / "graph.db"

    if not graph_path.is_file():
        print(json.dumps({"ok": False, "error": "graph.db not found — no entities extracted yet"},
                         ensure_ascii=False))
        return

    cache = CacheConnection(str(graph_path))
    repo = GraphRepository(cache)

    try:
        sub = args.graph_command

        if sub == "search":
            results = repo.search_entities(args.query, limit=args.limit)
            entities = []
            for r in results:
                sg = repo.get_subgraph(r["id"], max_depth=args.depth)
                entities.append({
                    "entity": r,
                    "subgraph": {
                        "neighbors": sg["neighbors"],
                        "relations": sg["relations"],
                    },
                })
            json.dump({"ok": True, "results": entities}, sys.stdout,
                      indent=2, ensure_ascii=False)

        elif sub == "trace":
            matches = repo.search_entities(args.query, limit=1)
            if not matches:
                json.dump({"ok": False, "error": f"entity not found: {args.query}"},
                          sys.stdout, ensure_ascii=False)
                return
            chain = repo.trace_decision_chain(matches[0]["id"], max_depth=args.depth)
            json.dump({"ok": True, "entity": matches[0], "chain": chain},
                      sys.stdout, indent=2, ensure_ascii=False)

        elif sub == "stats":
            stats = repo.stats()
            json.dump({"ok": True, "stats": stats}, sys.stdout,
                      indent=2, ensure_ascii=False)

        elif sub == "conflicts":
            limit = getattr(args, "limit", 50)
            conflicts = repo.get_conflicts(limit=limit)
            json.dump({
                "ok": True,
                "conflict_count": len(conflicts),
                "conflicts": [{
                    "entity_id": c["entity_id"],
                    "payload": c["payload"],
                    "created_at": c["created_at"],
                } for c in conflicts],
            }, sys.stdout, indent=2, ensure_ascii=False)

    finally:
        cache.close()


def cmd_doctor(args: argparse.Namespace) -> None:
    """全面自检：环境 + 配置 + hook + DB + OV 一次查清."""
    do_fix = getattr(args, "fix", False)

    print()
    print(f"  🏥 cc-star v{__version__} doctor — 全面自检")
    print(f"  ───────────────────────────────────")
    if do_fix:
        print(f"  🔧 --fix 模式: 自动修复常见问题")
    print()

    cfg_mgr = _get_config_manager()
    config_dir = cfg_mgr.config_path.parent
    data_dir = cfg_mgr.data_dir
    all_ok = True
    fixed_items: list[str] = []

    # 1. 配置
    config_file = config_dir / "config.yaml"
    if config_file.is_file():
        print(f"  ✅ 配置文件  {config_file}")
    elif do_fix:
        print(f"  ❌ 配置文件缺失 — --fix: 运行 init ...")
        try:
            installer = HookInstaller(cfg_mgr)
            installer.install(agent_name="assistant", non_interactive=True)
            if config_file.is_file():
                fixed_items.append("配置文件 (init)")
                print(f"  ✅ 已修复: 配置文件已通过 init 重建")
        except Exception as e:
            print(f"  ❌ 自动修复失败: {e}")
            all_ok = False
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
        print(f"  ⚪ 数据库文件不存在（新装机正常，使用后会自动创建）")

    # 3. Hook 脚本
    hooks_dir = config_dir / "hooks"
    expected = ["session_start.py", "inject.py", "store.py", "summary.py", "compact.py", "graph_extract.py"]
    if hooks_dir.is_dir():
        present = [p.name for p in hooks_dir.glob("*.py")]
        missing = [f for f in expected if f not in present]
        if not missing:
            print(f"  ✅ Hook 脚本  {len(present)}/{len(expected)} 齐全")
        elif do_fix:
            print(f"  ⚠️ Hook 脚本缺失: {missing} — --fix: 重新渲染 ...")
            try:
                config = cfg_mgr.load()
                installer = HookInstaller(cfg_mgr)
                installer._register_hooks(hooks_dir, config)
                # Re-render hook scripts
                from cc_star.installer import TemplateRenderer, _get_template_vars
                renderer = TemplateRenderer(hooks_dir.parent.parent / "templates")
                vars = _get_template_vars(config)
                for tmpl_name in renderer.list_templates():
                    rendered = renderer.render(tmpl_name, vars)
                    output_name = tmpl_name.replace(".j2", "")
                    output_path = hooks_dir / output_name
                    output_path.write_text(rendered, encoding="utf-8")
                fixed_items.append(f"Hook 脚本 ({len(missing)} 个)")
                print(f"  ✅ 已修复: Hook 脚本已重新渲染")
            except Exception as e:
                print(f"  ❌ 自动修复失败: {e}")
                all_ok = False
        else:
            print(f"  ⚠️ Hook 脚本缺失 — {missing}")
            all_ok = False
    else:
        if do_fix:
            print(f"  ❌ Hook 目录缺失 — --fix: 运行 init ...")
            try:
                installer = HookInstaller(cfg_mgr)
                installer.install(agent_name="assistant", non_interactive=True)
                if hooks_dir.is_dir():
                    fixed_items.append("Hook 目录 (init)")
                    print(f"  ✅ 已修复: Hook 目录已通过 init 重建")
            except Exception as e:
                print(f"  ❌ 自动修复失败: {e}")
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

            # Check cc-star hooks specifically
            required = ["SessionStart", "UserPromptSubmit", "Stop", "SessionEnd", "PreCompact", "PostCompact"]
            missing_hooks = [h for h in required if h not in hooks]
            if missing_hooks:
                print(f"  🔴 cc-star HOOKS MISSING: {missing_hooks}")
                # Auto-restore from hooks.registry.json
                try:
                    from cc_star.installer import _restore_hooks_from_registry
                    result = _restore_hooks_from_registry(config_dir)
                    if result:
                        fixed_items.append("settings.json hooks (registry 恢复)")
                        print(f"  ✅ 已修复 — {result}")
                        # Re-check
                        try:
                            s2 = json.loads(settings_path.read_text(encoding="utf-8"))
                            hooks2 = s2.get("hooks", {})
                            still_missing = [h for h in required if h not in hooks2]
                            if still_missing:
                                print(f"  ⚠️ 恢复后仍缺失: {still_missing}")
                                all_ok = False
                            else:
                                print(f"  ✅ 全部 hook 已恢复")
                        except Exception:
                            all_ok = False
                    else:
                        print(f"  ❌ 自动恢复失败 — hooks.registry.json 不存在或格式异常")
                        print(f"     请重新运行 cc-star init --force 重新初始化")
                        all_ok = False
                except Exception as e:
                    print(f"  ❌ 自动恢复异常 — {e}")
                    all_ok = False
        except Exception as e:
            print(f"  ⚠️ 读取 settings.json 失败 — {e}")
    else:
        print(f"  ⚪ Claude Code settings.json 不存在（未安装 Claude Code?）")

    # 5. Context Graph (graph.db)
    graph_path = data_dir / "graph.db"
    if graph_path.is_file():
        try:
            from cc_star.graph.repository import GraphRepository
            graph_cache = CacheConnection(str(graph_path))
            graph_repo = GraphRepository(graph_cache)
            gs = graph_repo.stats()
            graph_cache.close()
            size_kb = graph_path.stat().st_size / 1024
            print(f"  ✅ Context Graph  {graph_path} ({size_kb:.0f}KB, "
                  f"{gs['entities']}实体, {gs['relations']}关系, "
                  f"{gs['events']}事件, {gs['failed_events']}失败)")
        except Exception as e:
            print(f"  ⚠️ Context Graph 异常 — {e}")
    else:
        print(f"  ⚪ Context Graph 未创建（首次运行后自动生成）")

    # 6. hooks.registry.json
    registry_path = config_dir / "hooks.registry.json"
    if registry_path.is_file():
        print(f"  ✅ hooks.registry.json  可恢复")
    else:
        print(f"  ⚪ hooks.registry.json 未创建（运行 cc-star init 生成）")

    # 7. 原生记忆
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

    # 8. 快照 / STATUS
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

    # 9. OpenViking
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
        if fixed_items:
            print(f"  🔧 本次自动修复: {', '.join(fixed_items)}")
    else:
        if do_fix and fixed_items:
            print(f"  🔧 本次自动修复: {', '.join(fixed_items)}")
        print(f"  ⚠️ 存在需要修复的项目，请按上述提示操作")
    print()


def cmd_scheduler(args: argparse.Namespace) -> None:
    """Windows Task Scheduler — manage consolidation timer task."""
    from cc_star.scheduler import register, status as sched_status, unregister

    result: dict[str, str | bool | dict] = {}

    if args.scheduler_command == "register":
        result = register(start_time=args.time)
    elif args.scheduler_command == "unregister":
        result = unregister()
    elif args.scheduler_command == "status":
        result = sched_status()

    msg = result.get("message", str(result))
    print()
    print(f"  📅 cc-star scheduler — {args.scheduler_command}")
    print(f"  ───────────────────────────────────")
    print(f"  {msg}")
    print()

    if not result.get("ok", False):
        sys.exit(1)


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


# ── Profile ──


def cmd_profile(args: argparse.Namespace) -> None:
    """Show user behavior pattern profile."""
    from cc_star.memos import patterns as pattern_capture

    action = args.profile_command
    cfg_mgr = _get_config_manager()
    cfg = cfg_mgr.load()
    agent_name = cfg.get("agent", {}).get("name", "assistant")

    if action == "list" or action is None:
        _profile_list()
    elif action == "clear":
        _profile_clear(args.value)
    elif action == "stats":
        _profile_stats()
    else:
        print(f"  Unknown profile command: {action}")


def _profile_list() -> None:
    """Display all tracked patterns in a table."""
    groups = pattern_capture.load_all_counters()
    if not groups:
        print("  📋 用户行为画像 — 暂无记录")
        return

    # Flatten all records
    all_recs: list[dict] = []
    for recs in groups.values():
        all_recs.extend(recs)

    if not all_recs:
        print("  📋 用户行为画像 — 暂无记录")
        return

    # Sort by count desc
    all_recs.sort(key=lambda r: r.get("count", 0), reverse=True)

    print(f"  📋 用户行为画像 ({len(all_recs)} 条)")
    print()
    # Header
    print(f"  {'模式':<14} {'值':<30} {'命中':<6} {'状态':<10}")
    print(f"  {'─'*14} {'─'*30} {'─'*6} {'─'*10}")
    for rec in all_recs:
        label = rec.get("label", rec.get("pattern_id", "?"))[:12]
        value = rec.get("value", "?")[:28]
        count = rec.get("count", 0)
        status = "✅ 已固化" if rec.get("promoted") else f"🔄 {count}/3"
        print(f"  {label:<14} {value:<30} {count:<6} {status:<10}")

    print()
    print("  ﹒promoted = 已晋升到原生记忆，SessionStart 自动注入")
    print("  ﹒计数 ≥ 3 时自动晋升")


def _profile_clear(pattern_value: str | None) -> None:
    """Clear a specific pattern or all patterns."""
    if not pattern_value or pattern_value == "--all":
        # Confirm
        from pathlib import Path
        counter_path = Path(pattern_capture.get_counter_path())
        if counter_path.is_file():
            counter_path.write_text("", encoding="utf-8")
            print("  ✅ 所有行为模式已清除")
        else:
            print("  ⚠️ 暂无记录")
        return

    groups = pattern_capture.load_all_counters()
    found = False
    for pid, recs in list(groups.items()):
        groups[pid] = [r for r in recs if r.get("value") != pattern_value]
        if len(groups[pid]) != len(recs):
            found = True
        if not groups[pid]:
            del groups[pid]
    if found:
        pattern_capture.save_all_counters(groups)
        print(f"  ✅ 已清除模式: {pattern_value}")
    else:
        print(f"  ⚠️ 未找到: {pattern_value}")


def _profile_stats() -> None:
    """Show pattern statistics."""
    groups = pattern_capture.load_all_counters()
    total = sum(len(recs) for recs in groups.values())
    promoted = sum(
        1 for recs in groups.values() for r in recs if r.get("promoted")
    )
    pending = sum(
        1 for recs in groups.values() for r in recs if r.get("count", 0) >= 3 and not r.get("promoted")
    )
    high_count = sum(
        1 for recs in groups.values() for r in recs if r.get("count", 0) >= 5
    )
    print(f"  📊 模式统计")
    print(f"  总记录: {total}")
    print(f"  已固化: {promoted}")
    print(f"  待晋升: {pending}")
    print(f"  高置信 (≥5次): {high_count}")
    print(f"  覆盖类别: {len(groups)}")


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
    promote_p.add_argument("--backfill-embeddings", action="store_true",
                           help="Backfill missing embeddings for all traces without one")

    # graph
    graph_p = sub.add_parser("graph", help="Context graph operations")
    graph_sub = graph_p.add_subparsers(dest="graph_command", required=True)

    graph_search_p = graph_sub.add_parser("search", help="Search entities and show subgraph")
    graph_search_p.add_argument("query", help="Entity name or keyword")
    graph_search_p.add_argument("--limit", type=int, default=10,
                                help="Max search results (default: 10)")
    graph_search_p.add_argument("--depth", type=int, default=2,
                                help="Subgraph depth (default: 2)")

    graph_trace_p = graph_sub.add_parser("trace", help="Trace decision chain for an entity")
    graph_trace_p.add_argument("query", help="Entity name to trace")
    graph_trace_p.add_argument("--depth", type=int, default=10,
                               help="Max trace depth (default: 10)")

    graph_sub.add_parser("stats", help="Context graph statistics")

    conflicts_p = graph_sub.add_parser("conflicts", help="Show type conflicts between entities")
    conflicts_p.add_argument("--limit", type=int, default=50,
                             help="Max conflicts to show (default: 50)")

    # profile
    profile_p = sub.add_parser("profile", help="Show/manage user behavior pattern profile")
    profile_sub = profile_p.add_subparsers(dest="profile_command")

    profile_sub.add_parser("list", help="List all tracked patterns")
    profile_p.set_defaults(profile_command="list")

    profile_clear_p = profile_sub.add_parser("clear", help="Clear a specific pattern or all patterns")
    profile_clear_p.add_argument("value", nargs="?", default="--all",
                                  help="Pattern value to clear (default: --all)")

    profile_sub.add_parser("stats", help="Pattern statistics")

    # doctor
    doctor_p = sub.add_parser("doctor", help="全面自检：环境 + 配置 + hook + DB + OV 一次查清")
    doctor_p.add_argument("--fix", action="store_true",
                          help="自动修复常见问题（配置文件缺失、hook 缺失、hook 注册丢失等）")

    # scheduler (Windows Task Scheduler)
    scheduler_p = sub.add_parser("scheduler",
                                  help="管理 Windows 计划任务（凌晨 3:00 consolidation 巩固）")
    scheduler_sub = scheduler_p.add_subparsers(dest="scheduler_command", required=True)

    register_p = scheduler_sub.add_parser("register",
                                          help="注册计划任务（默认 03:00，可自定义时间）")
    register_p.add_argument("--time", default="23:00",
                            help="执行时间，HH:MM 24 小时格式（默认 23:00）")
    scheduler_sub.add_parser("unregister",
                              help="删除已注册的计划任务")
    scheduler_sub.add_parser("status",
                              help="查询计划任务状态")

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
    elif args.command == "graph":
        cmd_graph(args)
    elif args.command == "doctor":
        cmd_doctor(args)
    elif args.command == "profile":
        cmd_profile(args)
    elif args.command == "scheduler":
        cmd_scheduler(args)


if __name__ == "__main__":
    main()
