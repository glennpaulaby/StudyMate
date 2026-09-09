import logging
from typing import List, Optional, Tuple
from study_agent.memory.embeddings import Embedder, pack_embedding
from study_agent.memory.rerank import Reranker
from study_agent.memory.store import FactRecord, MemoryStore

logger = logging.getLogger(__name__)

BACKFILL_BATCH = 100


def _backfill_missing_vectors(store: MemoryStore, embedder: Embedder) -> None:
    """Embeds up to BACKFILL_BATCH vector-less facts for the active model."""
    missing = store.get_facts_missing_embeddings(embedder.model, limit=BACKFILL_BATCH)
    if not missing:
        return
    vectors = embedder.embed([f"{f.topic}: {f.fact}" for f in missing])
    for fact, vector in zip(missing, vectors):
        store.set_fact_embedding(fact.id, pack_embedding(vector), embedder.model)


def _apply_rerank(
    merged: List[FactRecord], query: str, reranker: Reranker
) -> Tuple[List[FactRecord], bool]:
    """Reorders merged hits by reranker relevance; keeps order on any failure."""
    if len(merged) < 2:
        return merged, False
    try:
        documents = [f"{f.topic}: {f.fact}" for f in merged]
        ranked = reranker.rerank(query, documents)
        order = [i for i, _ in ranked if 0 <= i < len(merged)]
        order += [i for i in range(len(merged)) if i not in set(order)]
        return [merged[i] for i in order], True
    except Exception as e:
        logger.warning(f"Rerank failed, keeping hybrid order: {e}")
        return merged, False


def hybrid_recall(
    store: MemoryStore,
    query: str,
    embedder: Optional[Embedder] = None,
    limit: int = 5,
    reranker: Optional[Reranker] = None,
) -> Tuple[List[FactRecord], bool]:
    """LIKE page union vector page for a query, optionally reranked.

    LIKE hits come first (precise), then semantic-only extras up to `limit`,
    then an optional reranker reorder. Returns (records, semantic_used).
    Any embedding/rerank failure degrades gracefully instead of raising —
    tools must never throw to the LLM.
    """
    like_hits = store.recall_facts(query, limit=limit)
    if embedder is None or not (query or "").strip():
        return like_hits, False
    try:
        _backfill_missing_vectors(store, embedder)
        query_vector = embedder.embed([query.strip()])[0]
        semantic = store.recall_semantic_vector(
            pack_embedding(query_vector), embedder.model, limit=limit
        )
    except Exception as e:
        logger.warning(f"Semantic recall failed, LIKE-only results: {e}")
        return like_hits, False
    seen = {f.id for f in like_hits}
    merged = list(like_hits)
    for fact, _distance in semantic:
        if len(merged) >= limit:
            break
        if fact.id not in seen:
            seen.add(fact.id)
            merged.append(fact)
    if reranker is not None:
        merged, _reranked = _apply_rerank(merged, query.strip(), reranker)
    return merged, True
