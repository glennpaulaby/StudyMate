from pathlib import Path
from study_agent.memory.embeddings import MockEmbedder
from study_agent.memory.rerank import MockReranker, parse_rerank_response
from study_agent.memory.retrieval import hybrid_recall
from study_agent.memory.store import MemoryStore
from study_agent.memory.tools import register_memory_tools
from study_agent.tools.base import ToolRegistry


def test_parse_live_rerank_shape():
    """Verify the real /rerank envelope (results/index/relevance_score) parses, sorted desc."""
    payload = {
        "id": "score-abc",
        "results": [
            {"index": 0, "relevance_score": 0.95, "document": {"text": "Paris."}},
            {"index": 2, "relevance_score": 0.13, "document": {"text": "Eiffel."}},
            {"index": 1, "relevance_score": 0.00, "document": {"text": "Berlin."}},
        ],
        "meta": {"billed_units": {"total_tokens": 1}},
    }
    assert parse_rerank_response(payload, 3) == [(0, 0.95), (2, 0.13), (1, 0.0)]


def test_parse_rerank_shape_variants_and_garbage():
    """Verify alternate envelopes parse; garbage raises instead of silently misordering."""
    assert parse_rerank_response({"rankings": [{"index": 1, "score": 0.7}, {"index": 0, "score": 0.2}]}, 2) == [
        (1, 0.7),
        (0, 0.2),
    ]
    assert parse_rerank_response([0.3, 0.9], 2) == [(1, 0.9), (0, 0.3)]
    for bad in ({"nope": 1}, {"results": [{"index": 9, "score": 1.0}]}, {"results": "x"}, 42):
        try:
            parse_rerank_response(bad, 2)
            raise AssertionError(f"expected ValueError for {bad!r}")
        except ValueError:
            pass


def test_mock_reranker_scripted_order():
    """Verify MockReranker orders by scripted scores and records calls (no network)."""
    mock = MockReranker(scores={"b-doc": 0.9, "a-doc": 0.1}, model="mock-r")
    assert mock.model == "mock-r"
    assert mock.rerank("q", ["a-doc", "b-doc", "c-doc"]) == [(1, 0.9), (0, 0.1), (2, 0.0)]
    assert mock.calls == [("q", ["a-doc", "b-doc", "c-doc"])]


def test_hybrid_rerank_reorders_and_survives_outage(tmp_path: Path):
    """Verify rerank reorders merged hits; broken reranker keeps hybrid order."""
    from study_agent.memory.rerank import Reranker

    store = MemoryStore(tmp_path / "memory.db")
    try:
        store.store_fact("graphs", "alpha fact")
        store.store_fact("graphs", "beta fact")
        mock_embed = MockEmbedder(
            vectors={
                "graphs: alpha fact": [1.0, 0.0],
                "graphs: beta fact": [1.0, 0.0],
                "q": [1.0, 0.0],
            },
            dim=2,
        )
        reranker = MockReranker(scores={"graphs: beta fact": 0.9, "graphs: alpha fact": 0.1})
        merged, used = hybrid_recall(store, "q", mock_embed, limit=5, reranker=reranker)
        assert used is True
        assert [f.fact for f in merged] == ["beta fact", "alpha fact"]
        assert reranker.calls and reranker.calls[0][0] == "q"

        class BrokenReranker(Reranker):
            @property
            def model(self) -> str:
                return "broken"

            def rerank(self, query, documents):
                raise RuntimeError("rerank down")

        merged2, used2 = hybrid_recall(store, "graphs", mock_embed, limit=5, reranker=BrokenReranker())
        assert used2 is True  # semantic still used; order simply not reranked
        assert len(merged2) == 2
    finally:
        store.close()


def test_recall_tool_with_reranker_orders_results(tmp_path: Path):
    """Verify memory_recall end-to-end ordering through the tool registry."""
    store = MemoryStore(tmp_path / "memory.db")
    try:
        registry = ToolRegistry()
        mock_embed = MockEmbedder(
            vectors={
                "t: first": [1.0, 0.0],
                "t: second": [1.0, 0.0],
                "q": [1.0, 0.0],
            },
            dim=2,
        )
        reranker = MockReranker(scores={"t: second": 0.9, "t: first": 0.1})
        register_memory_tools(registry, store, mock_embed, reranker)
        store.store_fact("t", "first")
        store.store_fact("t", "second")
        res = registry.execute("memory_recall", {"query": "q"})
        assert res.index("second") < res.index("first")
    finally:
        store.close()
