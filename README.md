# cc-star

**不是日记本，是认知引擎。**
**Not a notebook. A cognition engine.**

Claude Code 的原生记忆是便利贴——你写什么它记什么。
cc-star 是数字大脑——它理解*为什么*重要，让记忆自我进化。

```
pip install cc-star && cc-star init
# 30 秒 → 你的 Claude Code 开始从每次对话中学习
```

---

## 🎯 What's New in v0.3 — 类脑记忆系统

### 🔍 三源合一检索（Tri-Source Retrieval）

之前只搜 cache.db + OpenViking，现在原生记忆也纳入检索。

```
用户输入 → FTS5(cache.db 对话记忆) + 关键词(原生核心记忆) + 语义(OpenViking 团队共享)
         → RRF 三源融合排序 → 注入额外上下文
```

不遗漏任何维度的记忆。

### ⬆️ 记忆自动晋升 L2→L3（Auto Promotion）

对话存储后自动判断：高频/重要内容 → 渲染 markdown → 写入 `~/.claude/memory/`。
下次会话 Claude Code 自动加载，完全无需手动管理。

触发条件：含架构/决策/协议/方案等关键词 + 内容长度 > 150 字 + 7 天冷却期。

### 🧹 记忆生命周期管理（Lifecycle）

| 机制 | 行为 | 阈值 |
|------|------|------|
| cache.db 超限回收 | 删除最旧 traces | > 1GB 触发，回收至 70% |
| 原生记忆去重 | 内容哈希比对 → 重命名 .bak | 自动 |
| 热记忆扫描 | 综合评分（关键词密度 + 长度）→ 晋升 L3 | 每周建议一次 |

### ⚡ 配置动态化（Dynamic Config）

改 `~/.cc-star/config.yaml` 即时生效，无需 re-init。
支持环境变量覆盖（`CC_STAR_*`）。

---

## Quick Start

```bash
# 安装
pip install cc-star

# 初始化（30 秒）
cc-star init

# 全面自检
cc-star doctor

# 搜索记忆
cc-star search "之前那个架构方案是怎么设计的"

# 记忆维护（建议每周一次）
cc-star promote

# 查看状态
cc-star status
```

---

## Commands

| Command | Description |
|---------|-------------|
| `cc-star init` | 初始化记忆系统（中文引导 + 自检） |
| `cc-star doctor` | 全面自检（配置/hook/DB/OV 一次查清） |
| `cc-star status` | 查看运行状态 |
| `cc-star search <query>` | 搜索记忆 |
| `cc-star promote` | 记忆维护（cache 回收 + 去重 + 热晋升） |
| `cc-star config` | 查看配置 |
| `cc-star config <key> <value>` | 修改配置 |
| `cc-star uninstall` | 移除 hook |

---

## Architecture v0.3

```
┌──────────────────────────────────────────────────────────────┐
│                        cc-star v0.3                           │
│  ┌──────────────────────────────────────────────────────┐    │
│  │                    Tril-Source Retrieval                │    │
│  │  ┌─────────────────┐  ┌──────────────┐  ┌──────────┐  │    │
│  │  │  cache.db FTS5  │  │  原生记忆     │  │OpenViking│  │    │
│  │  │  L2 短期对话记忆 │  │  L3 核心知识   │  │ 团队共享  │  │    │
│  │  └────────┬────────┘  └──────┬───────┘  └─────┬────┘  │    │
│  │           └──────────────────┼─────────────────┘       │    │
│  │                      ┌──────▼───────┐                  │    │
│  │                      │  RRF Merge   │                  │    │
│  │                      │  融合排序     │                  │    │
│  │                      └──────┬───────┘                  │    │
│  │                             ▼                          │    │
│  │                    additionalContext                    │    │
│  └──────────────────────────────────────────────────────┘    │
│                                                              │
│  ┌──────────┐  ┌────────────────────────────────────────┐   │
│  │  5 Hooks  │  │  Memory Lifecycle                      │   │
│  │  Session  │  │  ┌─────────┐  ┌──────────┐  ┌──────┐  │   │
│  │  Start    │  │  │store.py │→│promote.py│→│ dedup │  │   │
│  │  Inject   │  │  │ 存储对话 │  │ 晋升+回收  │  │ 去重  │  │   │
│  │  Store    │  │  └─────────┘  └──────────┘  └──────┘  │   │
│  │  Summary  │  └────────────────────────────────────────┘   │
│  │  Compact  │                                              │
│  └──────────┘                                              │
└──────────────────────────────────────────────────────────────┘
```

### Hook Flow

- **SessionStart** — 环境自检 + 上次会话摘要
- **UserPromptSubmit (inject)** — **三源合一检索**：FTS5 + 原生记忆关键词 + OV 语义 → RRF 融合
- **Stop (store)** — 存储对话 + **自动判断是否晋升到原生记忆**
- **SessionEnd (summary)** — 会话摘要 + OpenViking 批量同步
- **PreCompact/PostCompact (compact)** — 压缩保护（配置动态加载）

---

## Configuration

Config file: `~/.cc-star/config.yaml`

```yaml
agent:
  name: assistant
  tags: ["claude-code"]
storage:
  path: ~/.cc-star/data
memory:
  max_inject: 5           # 每次注入最多 5 条对话记忆
  max_inject_native: 3     # 每次注入最多 3 条核心记忆
  memory_path: ""          # 原生记忆目录（设 ~/.claude/memory 启用三源检索）
  status_path: ""          # STATUS.md 路径（用于压缩保护）
  snapshot_path: ""        # OV 快照路径（用于压缩保护）
  promote_enabled: true    # 是否启用记忆晋升
  promote_min_length: 150  # 晋升最小内容长度
  promote_cooldown_days: 7 # 晋升冷却期
  max_cache_mb: 1000       # cache.db 上限（超限自动回收）
ov:
  enabled: false
  url: ""
  sync_batch: 50
hooks:
  timeout_inject: 10
  timeout_store: 15
  timeout_summary: 30
  timeout_session_start: 10
  timeout_compact_save: 5
  timeout_compact_restore: 10
```

---

## Windows Users

See the **[Windows Installation Guide](docs/windows-install.md)** for known pitfalls.

---

## 🧠 Why cc-star?

| 对比项 | Claude Code 原生记忆 | cc-star v0.2 | cc-star v0.3 🆕 |
|--------|-------------------|-------------|----------------|
| 检索方式 | 全量加载内存 | FTS5 + OV | **FTS5 + 原生 + OV 三源** |
| 对话存储 | ❌ 无 | ✅ | ✅ |
| 记忆晋升 | ❌ 手动 | ❌ | ✅ **自动** |
| 压缩保护 | ❌ | ✅ compact.py | ✅ **配置动态化** |
| 缓存维护 | ❌ | ❌ | ✅ **自动回收** |
| 团队共享 | ❌ | ✅ OV | ✅ OV |
| 配置生效 | 即时 | 需 re-init | ✅ **即时** |

---

## License

AGPL-3.0 — see [LICENSE](LICENSE)
