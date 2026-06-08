# cc-star

**不是日记本，是认知引擎。**
**Not a notebook. A cognition engine.**

---

## 🤷 为什么选 cc-star 不选别的？

**你的 Claude Code 每次启动都是一张白纸。**
项目架构得重新解释，之前的决策得重新翻，踩过的坑还得再踩一遍。

原生记忆是便利贴——你写什么它记什么，不写就没有。
其他记忆插件呢？装 MCP、配端口、挂后台、常驻进程……记忆还没用上，先运维上了。

**cc-star 不一样。**

```bash
pip install cc-star && cc-star init
# 30 秒 → 你的 Claude Code 开始从每次对话中自动学习
```

- **零 MCP 依赖** — 不装服务、不配端口、不挂后台。装完即用。
- **三源合一检索** — 对话记忆 + 核心知识 + 团队共享，一次搜全。
- **自动成长** — 高频内容自动晋升到原生记忆，下次会话零延迟加载。
- **零维护** — 自动回收、自动去重、自动晋升。装完不用管。

> **原生记忆是便利贴，claude-mem 是笔记本，cc-star 是会自我进化的数字大脑。**

---

## 🚀 5 分钟快速上手

### 第 1 分钟：安装

```bash
pip install cc-star
```

一条命令，无其他依赖。不需要 MCP 配置、不需要常驻进程、不需要额外端口。

### 第 2 分钟：初始化

```bash
cc-star init
```

自动完成：
- 创建 `~/.cc-star/config.yaml`（中文引导）
- 初始化 SQLite 数据库
- 安装 5 个 Claude Code hook（SessionStart / Inject / Store / Summary / Compact）
- 写入初始核心记忆

### 第 3 分钟：验证

```bash
cc-star doctor
```

全面自检：配置 ✅ → Hook ✅ → 数据库 ✅ → OpenViking（可选）✅

看到全绿就可以关掉了，之后**不用再管它**。

### 第 4-5 分钟：正常使用

**你什么也不用做。**

每次对话时 cc-star 自动：
1. **检索** — 从历史对话 + 核心知识 + 团队共享中找到相关上下文
2. **注入** — 注入到当前会话，Claude 直接"想起来"
3. **存储** — 对话结束后自动存储
4. **晋升** — 高频/重要内容自动提升到原生记忆

### 想深入？

```bash
cc-star search "之前那个架构方案是怎么设计的"   # 搜索记忆
cc-star promote                                  # 手动维护（可选）
cc-star status                                   # 查看运行状态
cc-star config memory.promote_enabled false       # 改配置即时生效
```

---

## 📊 与社区方案对比

| 对比维度 | cc-star v0.3 🆕 | claude-mem | claude-mem-lite | paradigm-memory | continuity-v2 | Nemp |
|---------|:--------------:|:----------:|:--------------:|:--------------:|:-------------:|:----:|
| **安装复杂度** | `pip install` ❐ `init` ⏤ 30 秒 | 需 MCP 配置 + 常驻端口 37777 | npx 启动 MCP 服务 | 脚本安装 + MCP 配置 | git clone + MCP 配置 | `/plugin` 两行 |
| **MCP 依赖** | ❌ **零依赖** | ✅ 需 MCP 服务 | ✅ 需 MCP 服务 | ✅ 需 MCP 服务 | ✅ 需 MCP 服务 | ❌ 零依赖 |
| **自动晋升** | ✅ **唯一** | ❌ | ❌ | ❌ 半自动 | ❌ | ❌ |
| **团队共享** | ✅ OpenViking | ❌ 仅本地 | ❌ 仅本地 | ❌ 仅本地 | ❌ 仅本地 | ❌ 仅本地 |
| **Windows 支持** | ✅ 原生 | ✅ | ❌ | ✅ | ✅ | ✅ |
| **维护成本** | **零维护** | 监控端口进程 | 低 | 手动 dream 合并 | 低 | 低 |
| **检索方式** | FTS5 + 关键词 + 语义 **三源融合** | BM25 + 向量 | FTS5 + TF-IDF | 认知树激活 | FTS5 + 语义 | 文件遍历 |
| **后台进程** | **无** | 常驻 Express | 按需 spawn | 常驻 stdio | 常驻 stdio | **无** |

**两张差异化王牌：**
- 🃏 **零 MCP 依赖** — 唯一不依赖 MCP 服务的记忆方案。装完即用，无端口、无进程、无崩溃风险。
- 🃏 **认知管道 L1→L3** — 唯一实现"对话→L2 cache→L3 原生记忆"自动晋升的方案。记忆会随着使用自动成长。

---

## 🏗️ 架构

### 三源融合检索

```
用户输入
    ↓
┌─ inject hook ──────────────────────────────────┐
│                                                 │
│  ① FTS5(cache.db 对话记忆)  ← 短期      │
│  ② 关键词(原生核心记忆)       ← 长期      │
│  ③ 语义(OpenViking 团队共享)  ← 团队     │
│                                                 │
│  → RRF 三源融合排序                             │
│  → 注入当前会话                                 │
└─────────────────────────────────────────────────┘
```

### 记忆生命周期

```
对话结束
    ↓
┌─ store hook ──────────────────────────────────┐
│  → cache.db (L2 短期对话记忆)                   │
│  → 高频/重要？→ 晋升原生记忆 (L3 核心知识)       │
│  → OpenViking 同步（可选）                      │
└─────────────────────────────────────────────────┘
    ↓ 自动（无需触发）
┌─ promote 管道 ──────────────────────────────────┐
│  → cache.db 超限自动回收（>1GB 触发）            │
│  → 原生记忆去重（内容哈希比对）                   │
│  → 热记忆扫描 → 自动晋升                         │
└──────────────────────────────────────────────────┘
```

### 5 个 Hook

| Hook | 时机 | 做什么 |
|------|------|--------|
| **SessionStart** | 会话启动 | 环境自检 + 加载上次摘要 |
| **UserPromptSubmit** | 每次提问 | **三源检索** → 注入上下文 |
| **Stop** | 会话结束 | 存储对话 + 判断晋升 |
| **SessionEnd** | 退出 | 生成摘要 + OV 同步 |
| **PreCompact/PostCompact** | 压缩保护 | 配置动态加载 |

---

## Commands

| Command | 功能 |
|---------|------|
| `cc-star init` | 初始化（中文引导 + 自检） |
| `cc-star doctor` | 全面自检 |
| `cc-star status` | 查看状态 |
| `cc-star search <query>` | 搜索记忆 |
| `cc-star promote` | 手动维护（回收 + 去重 + 晋升） |
| `cc-star config` | 查看配置 |
| `cc-star config <key> <value>` | 改配置（即时生效） |
| `cc-star uninstall` | 移除 hook |

---

## Configuration

`~/.cc-star/config.yaml` — 改完即时生效，无需 re-init。

```yaml
memory:
  max_inject: 5           # 每次注入最多 5 条对话记忆
  max_inject_native: 3    # 每次注入最多 3 条核心记忆
  promote_enabled: true   # 是否启用记忆晋升
  promote_min_length: 150 # 晋升最小长度
  promote_cooldown_days: 7 # 晋升冷却期
  max_cache_mb: 1000      # cache.db 上限
```

支持环境变量覆盖（`CC_STAR_*`），不改配置文件也行。

---

## Windows Users

Windows 用户看 **[Windows 安装指南](docs/windows-install.md)**。

---

## License

AGPL-3.0 — see [LICENSE](LICENSE)
