"""cc-star — Claude Code memory upgrade kit.

Includes:
- SQLite+FTS5 hot storage for conversation traces
- L1→L2→L3 cognitive pipeline (policy induction, skill crystallization, world model)
- hmem: Hierarchical MEMory with FAISS vector index, BM25 hybrid search, beam routing
- Ebbinghaus forgetting-curve decay with user feedback regulation
- Retrieval quality evaluation (recall@k, MRR, NDCG)
- Optional OpenViking cold sync
- Built-in viewer
"""

__version__ = "0.8.0"
