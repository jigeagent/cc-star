# cc-star v0.4.0 — 验证门控版

> 2026-06-13
> 从 v0.3.1 升级

---

## 新增

### 🆕 promote_gate.py — 验证门控模块

基于微软 SkillOpt 的 `evaluate_gate()` 纯函数逻辑移植：
- **三种门控结果** — `reject`（不优于当前）/ `accept`（优于当前）/ `accept_new_best`（超越历史最佳）
- **三种指标** — `hard`（刚性打分）/ `soft`（FTS5 相似度）/ `mixed`（加权融合，默认）
- **双线追踪** — `current_score` + `best_score` 持久化到 `gate_state.json`
- **拒绝缓冲** — 被 gate 拒绝的候选记入 `reject_log.jsonl`，不丢信息
- **可关闭** — `CC_STAR_GATE_ENABLED=false` 退化为旧版单向晋升

## 改进

### 📈 评分函数重写（_score_trace）

| 改进项 | 效果 |
|:-------|:------|
| 元数据降权 | bridge_context/JSON/XML 从 ~10 分降至 <2 分 |
| 对话加分 | 自然语言、问答结构、中文内容奖励 |
| top 20 元数据占比 | **100% → 0%** |

### 🤖 CJK 相似度计算（compute_soft_score）

`promote_gate.py` 新增中文 bigram 匹配作为 soft metric 输入。

## 兼容性

- [x] 默认开启门控（behavior change: 晋升从单向→验证）
- [x] 前向兼容：`--dry-run` 显示 gate 决策
- [x] 降级开关：`CC_STAR_GATE_ENABLED=false`
- [x] `gate_state.json` 首次运行自动初始化 baseline

## 技术债务

- 评分函数在真实高质量内容上测试饱和（646→594 项得 10.0 分）
- 需要引入独特性/信息密度因子做更细的刻度

---

**升级方式：** `pip install -e .` 或重新 clone
