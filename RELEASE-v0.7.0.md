# cc-star v0.7.0 — 记忆引擎化

> 2026-06-27
> 从 v0.6.0 升级

---

## 新增

### 🆕 Phase 4 — Context Graph（本地实体-关系图）

SQLite 递归 CTE 实现的轻量知识图谱：
- **实体抽取** — 对话中自动提取实体和关系（spaCy NER）
- **子图检索** — `cc-star graph search <query>` 展示关联子图
- **决策链追踪** — `cc-star graph trace <entity>` 回溯决策脉络
- **统计概览** — `cc-star graph stats` 实体/关系/事件数一览

### 🆕 Phase 5 — hooks.registry.json 自动恢复

解决 **settings.json 被覆盖导致全系统停摆** 的 🔴 风险：
- `_register_hooks()` 同步写入 `~/.cc-star/hooks.registry.json`
- `cc-star doctor` 检测到 hook 缺失时 **自动从 registry 恢复**
- 恢复前保留原有 settings.json 内容，仅合并 hook 配置
- 版本 + 时间戳追踪 registry 生命周期

### 🆕 Phase 6 — Windows Task Scheduler 集成

自动化凌晨 3:00 巩固任务：
- `cc-star promote` 纳入 consolidation_worker 自检引导
- Task Scheduler 注册/注销命令
- 与 23:00 备份窗口错开

---

## 改进

### 🏥 doctor 增强

| 改进项 | 效果 |
|:-------|:------|
| 版本号展示 | doctor banner 显示当前 v0.7.0 |
| `--fix` 参数 | 一键自动修复常见问题 |
| hooks 自动恢复 | 检测到缺失 → 从 registry 恢复 |
| hooks.registry.json 检测 | 独立检查并显示可恢复状态 |
| VERSION 文件 | 版本信息独立存储 |

### 🔧 CLI 新增

| 命令 | 用途 |
|:-----|:------|
| `cc-star doctor --fix` | 全面自检 + 自动修复 |
| `cc-star graph search` | 实体搜索 + 子图展示 |
| `cc-star graph trace`  | 决策链追踪 |
| `cc-star graph stats`  | 图谱概览 |

### 🧪 测试覆盖

| 模块 | 用例数 |
|:-----|:-------|
| 安装器 | 20 |
| CLI | 7 |
| 图谱 | 6 |
| 反事实/cdx-brain 集成 | — |
| **合计** | **33+** |

---

## 架构变化

```
v0.6.0 结构:
cc_star/
├── cache/          FTS5 + 向量检索
├── memos/          记忆管线
├── templates/      hook 模板
├── viewer/         本地可视化
├── ov/             OpenViking 同步
└── cli.py          CLI 入口

v0.7.0 新增:
├── graph/          Context Graph（SQLite CTE）
├── installer.py    hooks.registry.json 自动恢复
├── VERSION         版本文件
└── docs/           部署文档
```

---

## 技术债务

- graph_extract.py NER 依赖 spaCy zh_core_web_sm 离线模型（首次需下载）
- 冲突检测降级为基础合并规则，`[v0.7.1] 待增强`
- graph.db 与 OpenViking 联邦记忆互补不冲突，打通留 v0.8.0

---

## 升级方式

```bash
pip install -e D:/WorkBuddy/workspace/cc-star
cc-star init --force
cc-star doctor
```

> **注意：** `--force` 会重新注册 hook 并刷新 hooks.registry.json，已有数据不受影响。
