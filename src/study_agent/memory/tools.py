import logging
from typing import Any, Dict, List, Optional
from study_agent.memory.embeddings import Embedder, pack_embedding
from study_agent.memory.rerank import Reranker
from study_agent.memory.retrieval import hybrid_recall
from study_agent.memory.store import MemoryStore
from study_agent.observability.metrics import observe_memory
from study_agent.tools.base import BaseTool, ToolRegistry

logger = logging.getLogger(__name__)


class MemoryStoreTool(BaseTool):
    """Tool letting the LLM persist a durable user preference or learned fact."""

    def __init__(self, store: MemoryStore, embedder: Optional[Embedder] = None) -> None:
        self.store = store
        self.embedder = embedder

    @property
    def name(self) -> str:
        return "memory_store"

    @property
    def description(self) -> str:
        return (
            "Persist a durable user preference or learned fact for future sessions "
            "(e.g. 'struggles with Dijkstra', 'prefers Anki-style flashcards'). "
            "Use only for information worth remembering across restarts, not for "
            "ephemeral chat content. Exact duplicates are deduplicated automatically."
        )

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "Short topic label (e.g. 'dijkstra', 'study-preferences')",
                },
                "fact": {
                    "type": "string",
                    "description": "The durable fact text (max 2000 characters)",
                },
                "tags": {
                    "type": "string",
                    "description": "Optional comma-separated search tags (e.g. 'graphs,exam')",
                },
            },
            "required": ["topic", "fact"],
        }

    def execute(self, topic: str, fact: str, tags: str = "", **kwargs: Any) -> str:
        tag_list: List[str] = [t.strip() for t in (tags or "").split(",") if t.strip()]
        embedding: Optional[bytes] = None
        model: Optional[str] = None
        if self.embedder is not None:
            try:
                embedding = pack_embedding(self.embedder.embed([f"{topic.strip()}: {fact.strip()}"])[0])
                model = self.embedder.model
            except Exception as e:
                logger.warning(f"Fact embedding failed, storing text-only: {e}")
        try:
            record, created = self.store.store_fact(
                topic=topic, fact=fact, tags=tag_list, embedding=embedding, embedding_model=model
            )
        except ValueError as e:
            observe_memory("store_fact", "error")
            return f"Error: {e}"
        observe_memory("store_fact", "created" if created else "deduplicated")
        verb = "Stored" if created else "Already known (deduplicated)"
        vec_note = "" if embedding is not None else " (text-only, no vector)"
        return f"{verb} fact #{record.id} under topic '{record.topic}'{vec_note}."


class MemoryRecallTool(BaseTool):
    """Tool letting the LLM retrieve persisted facts just-in-time."""

    def __init__(
        self,
        store: MemoryStore,
        embedder: Optional[Embedder] = None,
        reranker: Optional[Reranker] = None,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.reranker = reranker

    @property
    def name(self) -> str:
        return "memory_recall"

    @property
    def description(self) -> str:
        return (
            "Recall persisted user preferences and learned facts relevant to the "
            "current task. Call this when personalizing (preferences, struggles, "
            "goals) before answering from general knowledge."
        )

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Topic or keyword to search for (empty lists recent facts)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max facts to return (default: 5, max: 20)",
                },
            },
            "required": ["query"],
        }

    def execute(self, query: str, limit: int = 5, **kwargs: Any) -> str:
        limit = max(1, min(int(limit), 20))
        facts, semantic_used = hybrid_recall(
            self.store, query, self.embedder, limit=limit, reranker=self.reranker
        )
        if not facts:
            observe_memory("recall", "empty")
            return f"No persisted facts found for '{query}'."
        observe_memory("recall", "semantic" if semantic_used else "keyword")
        lines = [f"Found {len(facts)} fact(s):"]
        for f in facts:
            tag_str = f" [tags: {', '.join(f.tags)}]" if f.tags else ""
            lines.append(f"- #{f.id} [{f.topic}] {f.fact}{tag_str}")
        total = self.store.count_facts(query)
        if total > len(facts):
            lines.append(f"({total - len(facts)} more; narrow the query or raise limit)")
        if self.embedder is not None and not semantic_used:
            lines.append("(vector search unavailable; showing keyword matches)")
        return "\n".join(lines)


def register_memory_tools(
    registry: ToolRegistry,
    store: Optional[MemoryStore],
    embedder: Optional[Embedder] = None,
    reranker: Optional[Reranker] = None,
) -> None:
    """Registers memory_store/memory_recall into the provided registry (no-op if store is None)."""
    if store is None:
        logger.warning("MemoryStore unavailable; memory tools not registered.")
        return
    registry.register(MemoryStoreTool(store, embedder))
    registry.register(MemoryRecallTool(store, embedder, reranker))
