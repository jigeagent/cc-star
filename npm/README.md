# cc-star-mcp 🧠

**Hierarchical memory MCP server for Claude Code**

BM25 + Vector hybrid search · 4-layer semantic hierarchy · Ebbinghaus forgetting curve · User feedback loop

[![npm version](https://img.shields.io/npm/v/cc-star-mcp)](https://www.npmjs.com/package/cc-star-mcp)
[![License](https://img.shields.io/badge/License-AGPL%203.0-blue.svg)](https://opensource.org/licenses/AGPL-3.0)

---

## Quick Start

### 1. Add to Claude Code

Add this to your Claude Code `settings.json`:

```json
{
  "mcpServers": {
    "cc-star": {
      "command": "npx",
      "args": ["-y", "cc-star-mcp"]
    }
  }
}
```

### 2. Initialize (first time only)

Run once to build the memory index:

```bash
npx cc-star-mcp --install
python -m cc_star hmem build
```

That's it. Claude Code will now have access to all your past conversations through hierarchical memory.

---

## What is cc-star?

cc-star is a **hierarchical memory system** that upgrades Claude Code's native file-based memory to a proper searchable database. It organizes 30,000+ conversation traces into a four-layer semantic hierarchy:

```
Domain (8) → Category (33) → Trace (3,863) → Episode (3,863)
```

### Key Features

| Feature | Description |
|---------|-------------|
| **4-Layer Hierarchy** | Domain → Category → Trace → Episode, built via K-means clustering |
| **Hybrid Search** | BM25 (exact keyword) + Vector (semantic) with RRF fusion |
| **FAISS Vector Index** | GPU/CPU-accelerated similarity search across 3,800+ episodes |
| **Beam Search Routing** | Multi-path retrieval prevents single-point failure |
| **Flat Fallback** | When hierarchy fails, global vector search kicks in |
| **Ebbinghaus Decay** | Memory weights decay over time — frequently accessed memories rise |
| **Feedback Loop** | User approval/rebuttal adjusts memory weights in real-time |
| **Evaluation** | Built-in recall@k / MRR / precision metrics |
| **OpenViking Sync** | Optional cross-agent memory sharing |

### Technical Stack

- **Vector Engine**: FAISS (IndexFlatIP)
- **Embedding**: BAAI/bge-small-zh-v1.5 (512d, Chinese-optimized) via fastembed/ONNX
- **Hybrid**: Pure-Python BM25 Okapi + FAISS Vector, RRF fusion
- **Storage**: SQLite with hierarchical node schema
- **Decay**: Ebbinghaus forgetting curve with access frequency bonus

---

## Tools

Once installed, Claude Code can use these MCP tools:

### `hmem_search(query, top_k)`
Search past conversations with hybrid retrieval. Returns relevant episodes with scores.

### `hmem_status`
Check memory health: node counts, index readiness, cache state.

### `hmem_build(confirm)`
Rebuild the hierarchy index from all available traces.

---

## Comparison

| Feature | cc-star | claude-mem-lite | oracle-memory | Oubli |
|---------|---------|----------------|---------------|-------|
| Hierarchy Depth | **4 layers** | 2 tiers | — | 4 levels |
| Vector Search | **FAISS** | — | — | LanceDB |
| Hybrid BM25+Vector | ✅ | ✅ FTS5+TF-IDF | ✅ | ✅ |
| Ebbinghaus Decay | **✅** | ❌ | ❌ | ❌ |
| User Feedback | **✅** | ❌ | ❌ | ❌ |
| Evaluation Metrics | **✅** | ❌ | ❌ | ❌ |
| Beam Search Routing | **✅** | ❌ | ❌ | ❌ |
| MCP Server | ✅ | ✅ | ✅ | ❌ |

---

## Requirements

- Python 3.10+
- Node.js 18+ (for npx)
- OS: Windows, macOS, Linux

---

## Development

```bash
git clone https://github.com/jigeagent/cc-star
cd cc-star
pip install -e ".[dev]"
python -m cc_star hmem build --domains 8 --categories 5
python -m cc_star.mcp  # Test MCP server
```

---

## License

AGPL-3.0-or-later — see [LICENSE](LICENSE).
