# cc-star

**Claude Code memory upgrade kit.**

Upgrade Claude Code's native `MEMORY.md` (a plain text file that gets constantly truncated) into a **digital-life memory system** — local SQLite hot storage + FTS5 retrieval + optional OpenViking cold sync.

```
pip install cc-star
cc-star init
# 30 seconds → permanent, searchable, offline-capable memory
```

## Features

- **Persistent storage** — every conversation turn saved to local SQLite database
- **Full-text search** — FTS5-powered memory retrieval across all past conversations
- **Context injection** — automatically injects relevant past memories before each prompt
- **Compression protection** — preserves critical context (MEMORY.md, STATUS.md) across Claude Code compaction events
- **Optional OpenViking sync** — cold storage with semantic search (install with `cc-star[ov]`)
- **Zero Claude Code config** — `cc-star init` handles all hook registration

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
Claude Code → 5 Hook Scripts → cache.db (SQLite+FTS5) → [optional] OpenViking
                ↑
           cc-star init
                ↓
          string.Template → ~/.cc-star/hooks/*.py
```

- **SessionStart** — checks OV health, shows last session summary
- **UserPromptSubmit (inject)** — FTS5 + optional OV semantic search, RRF merge, injects as `additionalContext`
- **Stop (store)** — reads transcript, extracts last turn, writes to cache.db
- **SessionEnd (summary)** — extracts session summary, batch syncs to OV
- **PreCompact/PostCompact (compact)** — preserves MEMORY.md / STATUS.md / OV snapshot across compression

## Dependencies

- **hermes-next** (>=0.2) — SQLite cache + FTS5 retrieval engine
- **pyyaml** (>=6.0) — YAML config parsing
- **openviking** (optional, >=0.3.22) — OpenViking cold storage client

## License

AGPL-3.0 — see [LICENSE](LICENSE)
