from pathlib import Path
from study_agent.core.context import ContextManager
from study_agent.memory.embeddings import MockEmbedder, pack_embedding
from study_agent.memory.retrieval import hybrid_recall
from study_agent.memory.store import MemoryStore
from study_agent.memory.tools import register_memory_tools
from study_agent.tools.base import ToolRegistry


def _embed2d(store: MemoryStore, fact_id: int, vector, model: str = "mock-v1") -> None:
    """Attaches a 2-D test vector to a fact."""
    assert store.set_fact_embedding(fact_id, pack_embedding(list(vector)), model) is True


def test_fact_store_and_recall_roundtrip(tmp_path: Path):
    """Verify facts persist with tags and are retrievable by keyword."""
    store = MemoryStore(tmp_path / "memory.db")
    try:
        record, created = store.store_fact(
            "dijkstra", "Struggles with edge relaxations", tags=["graphs", "exam"], source_thread="abc"
        )
        assert created is True
        assert record.id == 1
        assert record.tags == ["graphs", "exam"]
        assert record.source_thread == "abc"

        hits = store.recall_facts("relaxations")
        assert len(hits) == 1 and hits[0].topic == "dijkstra"
        assert store.count_facts() == 1
    finally:
        store.close()


def test_fact_dedupe_and_revive(tmp_path: Path):
    """Verify exact duplicates return the same row; archived dupes revive."""
    store = MemoryStore(tmp_path / "memory.db")
    try:
        first, created1 = store.store_fact("anki", "Prefers cloze deletions")
        second, created2 = store.store_fact("anki", "Prefers cloze deletions")
        assert created1 is True and created2 is False
        assert first.id == second.id
        assert store.count_facts() == 1

        assert store.archive_fact(first.id) is True
        assert store.count_facts() == 0
        revived, created3 = store.store_fact("anki", "Prefers cloze deletions")
        assert created3 is False and revived.id == first.id
        assert store.count_facts() == 1
    finally:
        store.close()


def test_fact_validation(tmp_path: Path):
    """Verify empty topic/fact and oversized facts are rejected."""
    store = MemoryStore(tmp_path / "memory.db")
    try:
        for topic, fact in [("", "x"), ("t", ""), ("  ", "x")]:
            try:
                store.store_fact(topic, fact)
                raise AssertionError(f"expected ValueError for {topic!r}/{fact!r}")
            except ValueError:
                pass
        try:
            store.store_fact("t", "x" * 2001)
            raise AssertionError("expected ValueError for oversized fact")
        except ValueError:
            pass
        assert store.count_facts() == 0
    finally:
        store.close()


def test_fact_pagination_and_archive(tmp_path: Path):
    """Verify paged recall/count and archive hiding."""
    store = MemoryStore(tmp_path / "memory.db")
    try:
        for i in range(12):
            store.store_fact("graphs", f"fact number {i}")
        assert store.count_facts() == 12
        p1 = store.recall_facts("", limit=10, offset=0)
        p2 = store.recall_facts("", limit=10, offset=10)
        assert len(p1) == 10 and len(p2) == 2
        assert p1[0].fact == "fact number 11"  # most recent first
        assert store.get_fact(p1[0].id).id == p1[0].id
        assert store.get_fact(99999) is None

        assert store.archive_fact(p1[0].id) is True
        assert store.archive_fact(p1[0].id) is False
        assert store.count_facts() == 11
        assert store.get_fact(p1[0].id) is None
    finally:
        store.close()


def test_fact_legacy_db_migration(tmp_path: Path):
    """Verify pre-facts DB files gain the facts table on open."""
    import sqlite3

    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    conn.commit()
    conn.close()

    store = MemoryStore(db)
    try:
        record, created = store.store_fact("new", "works after migration")
        assert created is True and record.id == 1
    finally:
        store.close()


def test_memory_tools_via_registry(tmp_path: Path):
    """Verify memory_store/memory_recall tools and error paths through ToolRegistry."""
    store = MemoryStore(tmp_path / "memory.db")
    try:
        registry = ToolRegistry()
        register_memory_tools(registry, store)
        names = {s["function"]["name"] for s in registry.list_schemas()}
        assert names == {"memory_store", "memory_recall"}

        res_store = registry.execute(
            "memory_store", {"topic": "dijkstra", "fact": "Struggles", "tags": "graphs, exam"}
        )
        assert "Stored fact #1" in res_store
        res_dupe = registry.execute("memory_store", {"topic": "dijkstra", "fact": "Struggles"})
        assert "deduplicated" in res_dupe

        res_recall = registry.execute("memory_recall", {"query": "dijkstra"})
        assert "Struggles" in res_recall and "#1" in res_recall
        res_miss = registry.execute("memory_recall", {"query": "nope"})
        assert "No persisted facts" in res_miss
        res_bad = registry.execute("memory_store", {"topic": "", "fact": "x"})
        assert res_bad.startswith("Error:")

        empty_registry = ToolRegistry()
        register_memory_tools(empty_registry, None)  # no-op, must not raise
        assert empty_registry.list_schemas() == []
    finally:
        store.close()


def test_vector_recall_ordering_and_archived_exclusion(tmp_path: Path):
    """Verify SQL cosine ordering, archived rows excluded from vector path."""
    store = MemoryStore(tmp_path / "memory.db")
    try:
        assert store.vector_available is True
        near, _ = store.store_fact("graphs", "near fact")
        far, _ = store.store_fact("other", "far fact")
        hidden, _ = store.store_fact("graphs", "hidden fact")
        _embed2d(store, near.id, [1.0, 0.0])
        _embed2d(store, far.id, [0.0, 1.0])
        _embed2d(store, hidden.id, [1.0, 0.0])
        assert store.archive_fact(hidden.id) is True

        hits = store.recall_semantic_vector(pack_embedding([1.0, 0.0]), "mock-v1", limit=10)
        assert [f.id for f, _ in hits] == [near.id, far.id]
        assert hits[0][1] < hits[1][1]  # distances ascending
    finally:
        store.close()


def test_vector_stale_model_invisible(tmp_path: Path):
    """Verify rows from a rotated-out embedding space never match vector queries."""
    store = MemoryStore(tmp_path / "memory.db")
    try:
        old, _ = store.store_fact("graphs", "old space fact")
        _embed2d(store, old.id, [1.0, 0.0], model="mock-old")
        assert store.recall_semantic_vector(pack_embedding([1.0, 0.0]), "mock-v1") == []
        # ...but LIKE still sees it (graceful degradation across rotations).
        assert len(store.recall_facts("old space")) == 1
    finally:
        store.close()


def test_vector_backfill_queue(tmp_path: Path):
    """Verify vector-less facts are reported for backfill, then disappear once set."""
    store = MemoryStore(tmp_path / "memory.db")
    try:
        rec, _ = store.store_fact("t", "no vector yet")
        assert [f.id for f in store.get_facts_missing_embeddings("mock-v1")] == [rec.id]
        _embed2d(store, rec.id, [0.5, 0.5])
        assert store.get_facts_missing_embeddings("mock-v1") == []
        assert store.set_fact_embedding(99999, pack_embedding([0.0, 1.0]), "mock-v1") is False
    finally:
        store.close()


def test_vector_columns_migrated_on_legacy_db(tmp_path: Path):
    """Verify pre-vector DB files gain embedding columns + vector_meta on open."""
    import sqlite3

    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    conn.execute(
        "CREATE TABLE facts (id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT NOT NULL, fact TEXT NOT NULL, "
        "tags TEXT NOT NULL DEFAULT '', source_thread TEXT, created_at TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0, "
        "UNIQUE(topic, fact))"
    )
    conn.commit()
    conn.close()

    store = MemoryStore(db)
    try:
        rec, created = store.store_fact("t", "works", embedding=pack_embedding([1.0, 0.0]), embedding_model="mock-v1")
        assert created is True
        hits = store.recall_semantic_vector(pack_embedding([1.0, 0.0]), "mock-v1")
        assert [f.id for f, _ in hits] == [rec.id]
    finally:
        store.close()


def test_hybrid_merge_like_first_then_semantic_extras(tmp_path: Path):
    """Verify hybrid puts LIKE hits first, then semantic-only extras, deduped."""
    store = MemoryStore(tmp_path / "memory.db")
    try:
        like_rec, _ = store.store_fact("dijkstra", "exact keyword hit")
        sem_rec, _ = store.store_fact("graphs", "paraphrased neighbor")
        mock = MockEmbedder(
            vectors={
                "dijkstra: exact keyword hit": [1.0, 0.0],
                "graphs: paraphrased neighbor": [0.9, 0.1],
                "unrelated query": [0.95, 0.05],
            },
            dim=2,
        )
        # "unrelated query" matches nothing by LIKE; both rows come from cosine,
        # closer vector first, no duplicates.
        merged, used = hybrid_recall(store, "unrelated query", mock, limit=5)
        assert used is True
        assert [f.id for f in merged] == [like_rec.id, sem_rec.id]
    finally:
        store.close()


def test_hybrid_degrades_to_like_on_embed_failure(tmp_path: Path):
    """Verify embedder outage never raises; LIKE results returned, flag False."""
    from study_agent.memory.embeddings import Embedder

    class BrokenEmbedder(Embedder):
        @property
        def model(self) -> str:
            return "broken"

        def embed(self, texts):
            raise RuntimeError("endpoint down")

    store = MemoryStore(tmp_path / "memory.db")
    try:
        store.store_fact("t", "keyword visible")
        merged, used = hybrid_recall(store, "keyword", BrokenEmbedder(), limit=5)
        assert used is False
        assert len(merged) == 1
        merged_none, used_none = hybrid_recall(store, "keyword", None, limit=5)
        assert used_none is False and len(merged_none) == 1
    finally:
        store.close()


def test_recall_tool_reports_vector_status(tmp_path: Path):
    """Verify memory_recall notes when vector search was unavailable."""
    from study_agent.memory.embeddings import Embedder

    class BrokenEmbedder(Embedder):
        @property
        def model(self) -> str:
            return "broken"

        def embed(self, texts):
            raise RuntimeError("endpoint down")

    store = MemoryStore(tmp_path / "memory.db")
    try:
        registry = ToolRegistry()
        register_memory_tools(registry, store, BrokenEmbedder())
        store.store_fact("t", "keyword visible")
        res = registry.execute("memory_recall", {"query": "keyword"})
        assert "keyword visible" in res
        assert "vector search unavailable" in res
    finally:
        store.close()


def test_inject_system_note(tmp_path: Path):
    """Verify startup fact injection merges into the single leading SYSTEM message.

    Strict chat templates (vLLM/LiteLLM) reject any SYSTEM entry past index 0,
    so the note must fold into messages[0], never append. Skips empties.
    """
    _ = tmp_path
    ctx = ContextManager(custom_system_prompt="System")
    assert ctx.inject_system_note("   ") is None
    assert len(ctx.get_messages()) == 1
    msg = ctx.inject_system_note("- [dijkstra] Struggles")
    assert msg is not None and msg.role.value == "system"
    assert len(ctx.get_messages()) == 1  # still exactly one SYSTEM message
    assert "System" in ctx.get_messages()[0].content
    assert "[dijkstra] Struggles" in ctx.get_messages()[0].content
    # Re-inject replaces; clear retains the note.
    ctx.inject_system_note("second note")
    assert ctx.get_messages()[0].content.count("second note") == 1
    assert "[dijkstra]" not in ctx.get_messages()[0].content
    ctx.clear()
    assert "second note" in ctx.get_messages()[0].content
    assert len(ctx.get_messages()) == 1
