# My contributions:
- continued memory work on feat/memory-facts-semantic: semantic facts table (topic, fact, tags, source_thread, UNIQUE dedupe with revive) plus memory_store/memory_recall tools and top-N startup auto-inject
- researched the official sqlite-vector docs (sqliteai/sqlite-vector) and integrated hybrid recall: LIKE matches first, then sqlite-vector cosine nearest neighbors, reordered by relevance
- added sqliteai-vector dep (pyproject plus requirements kept in sync); verified extension load,  archived-filtered vector queries on Windows
- converted embedding BLOBs to Float32 wire format with legacy migration; per-row embedding_model versioning so model rotations degrade to LIKE instead of corrupting rankings
- got API key granted all three models (chat, octen-embedding-8b dim 4096, qwen3-reranker-4b) after probing key_model_access_denied; locked the live rerank response envelope in tests with defensive parsing
- built reranker stage (httpx POST /rerank, MockReranker for offline tests) with graceful degradation at every stage; single INNKUBE_API_KEY documented in .env.example
- fixed live 400 "System message must be at the beginning": fact injection now merges into messages[0] instead of appending a second SYSTEM message (verified against the real REPL traceback)
- added workspace/artifacts/memory plus workspace/data to .gitignore and untracked the live dev database (233 threads) so local runs stop dirtying commits
- test: suite 82 -> 109 passed (embeddings, vector SQL ordering, stale-model, outage, paraphrase e2e);

# Team:
- Coordinated with teammates on the Week 2 split (my memory slices vs their MCP track) and aligned branch scopes
- Arranged the shared API key model grants and agreed on the single-key setup
