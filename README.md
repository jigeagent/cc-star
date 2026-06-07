# cc-star

**不是日记本，是认知引擎。**
**Not a notebook. A cognition engine.**

Claude Code's native memory is a notebook — it writes down what you did.
cc-star is an engine — it learns *why* it worked and turns that into reusable strategy.

Stop storing. Start growing.

```
pip install cc-star && cc-star init
# 30 seconds → your Claude Code starts learning from every conversation
```

Built on a **L1→L2→L3 cognitive pipeline**: raw conversations → rewarded patterns → crystallized skills. No other memory system for Claude Code does this.

## Features

- **Persistent storage** — every conversation turn saved to local SQLite database
- **Full-text search** — FTS5-powered memory retrieval across all past conversations
- **Context injection** — automatically injects relevant past memories before each prompt
- **Cognitive pipeline** — turns raw conversation history into structured knowledge:
  - **L1 Capture** — auto-collect every turn with metadata (tags, agent name, timestamps)
  - **Reward Engine** — apply outcome signals (success/failure/correction), backpropagate temporal discounts
  - **L2 Policy Induction** — extract reusable patterns from successful outcomes, build candidate pool with confidence scoring
  - **L3 Skill Crystallization** — promote high-frequency patterns into callable skills with test cases and trigger conditions
  - **World Model** — cluster concepts from memory, extract entity-relation triples for associative retrieval
- **Compression protection** — preserves critical context (MEMORY.md, STATUS.md) across Claude Code compaction events
- **Optional OpenViking sync** — cold storage with semantic search
- **Built-in viewer** — embedded SPA web UI to browse traces, policies, skills, concepts
- **Zero Claude Code config** — `cc-star init` handles all hook registration

> **Why cc-star stands out:** Most Claude Code memory systems stop at "store + search." cc-star goes further — it *thinks* about what it stores. The cognitive pipeline automatically distills raw conversations into policies, skills, and conceptual knowledge, turning your AI's experience into an ever-improving knowledge base. No other open-source memory system for Claude Code offers this capability.

## Quick Start

```bash
# Install
pip install cc-star

# Initialize (30 seconds)
cc-star init

# Start a new Claude Code session — memories will be automatically
# stored, searched, and injected

# Search your memory
cc-star search "how did we fix the auth bug?"

# Check status
cc-star status
```

## Commands

| Command | Description |
|---------|-------------|
| `cc-star init` | Initialize the memory system |
| `cc-star status` | Show memory system status |
| `cc-star search <query>` | Search local memory |
| `cc-star config` | View all configuration |
| `cc-star config <key> <value>` | Update configuration |
| `cc-star uninstall` | Remove hooks from Claude Code settings |

## Configuration

Config file: `~/.cc-star/config.yaml`

```yaml
agent:
  name: assistant
  tags: ["claude-code"]
storage:
  path: ~/.cc-star/data
memory:
  max_inject: 5
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

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                    cc-star                            │
│  ┌──────────┐  ┌─────────────────────────────────┐   │
│  │  CLI      │  │  5 Hook Scripts (auto-run)       │   │
│  │  init     │  │  ├─ SessionStart  → last session │   │
│  │  status   │  │  ├─ Inject        → FTS5+OV检索   │   │
│  │  search   │  │  ├─ Store         → 存储本轮对话   │   │
│  │  config   │  │  ├─ Summary       → 摘要+批量同步   │   │
│  │  viewer   │  │  └─ Compact       → 压缩保护       │   │
│  └──────────┘  └────────┬───────────┬───────────┘   │
│                          │           │                 │
│              ┌───────────▼───────────▼────┐            │
│              │    Cognitive Pipeline       │            │
│              │  L1 Capture → Reward → L2   │            │
│              │  Policy Induction → L3 Skill │            │
│              │  Crystallization → World     │            │
│              │  Model (concepts + triples)  │            │
│              └───────────┬─────────────────┘            │
│                          │                              │
│              ┌───────────▼──────────────────┐            │
│              │     Storage Layer              │            │
│              │  ┌─────────┐  ┌────────────┐   │            │
│              │  │ SQLite  │  │ OpenViking  │   │            │
│              │  │ + FTS5  │  │ (optional)  │   │            │
│              │  │ 热存     │  │ 冷存·语义检索 │   │            │
│              │  └─────────┘  └────────────┘   │            │
│              └─────────────────────────────────┘            │
└─────────────────────────────────────────────────────────────┘
```

### Hook Flow

- **SessionStart** — checks OV health, shows last session summary
- **UserPromptSubmit (inject)** — FTS5 + optional OV semantic search, RRF merge, injects as `additionalContext`
- **Stop (store)** — reads transcript, extracts last turn, writes to cache.db + L1 Capture
- **SessionEnd (summary)** — extracts session summary, batch syncs to OV
- **PreCompact/PostCompact (compact)** — preserves MEMORY.md / STATUS.md / OV snapshot across compression

### Cognitive Pipeline Flow

```
Raw Turn → L1 Capture → Reward Signal → L2 Policy Induction → L3 Skill Crystallization
                                         ↓
                                    World Model
                              (concept clustering +
                               entity-relation triples)
```

- **L1 Capture** — every turn is captured with turn index, session context, tags, and agent identity
- **Reward** — outcome signals (success/failure/correction) are applied with temporal discount backpropagation
- **L2 Policy** — successful patterns are clustered into policy candidates with confidence scores that update over time
- **L3 Skill** — high-confidence patterns are crystallized into executable skills with trigger conditions and test cases
- **World Model** — concepts are extracted and clustered; entity-relation triples enable associative retrieval

## Dependencies

- **httpx** (>=0.28) — HTTP client for OpenViking sync
- **pyyaml** (>=6.0) — YAML config parsing
- **numpy** (>=1.24) — vector operations for cognitive pipeline
- **openviking** (optional) — OpenViking cold storage client

## License

AGPL-3.0 — see [LICENSE](LICENSE)
