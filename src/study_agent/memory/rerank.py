"""
Semantic Document & Fact Reranker for StudyMate.

Provides cross-encoder scoring of candidate memory facts against query strings.
Supports InnKube's `/rerank` route (e.g. `qwen3-reranker-4b`) and deterministic
mock implementations for unit tests.
"""

import logging
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Tuple
import httpx

logger = logging.getLogger(__name__)


class Reranker(ABC):
    """Abstract relevance reranker (real or mock)."""

    @property
    @abstractmethod
    def model(self) -> str:
        """Reranker model identifier."""
        pass

    @abstractmethod
    def rerank(self, query: str, documents: List[str]) -> List[Tuple[int, float]]:
        """
        Scores documents against the query.

        Returns:
            List of (input_index, score) tuples sorted by score descending.
        """
        pass


def parse_rerank_response(data: object, count: int) -> List[Tuple[int, float]]:
    """
    Normalizes a /rerank JSON payload to [(index, score)] sorted descending.

    Defensive parser: accepts varying response shapes (results/rankings/data keys
    or a bare list; score, relevance_score, or relevance fields).
    """
    items: object = data
    if isinstance(data, dict):
        for key in ("results", "rankings", "data", "documents"):
            if isinstance(data.get(key), list):
                items = data[key]
                break
    if not isinstance(items, list):
        raise ValueError(f"Unparseable rerank payload: {type(data).__name__}")
    scored: List[Tuple[int, float]] = []
    for position, item in enumerate(items):
        if isinstance(item, dict):
            index = item.get("index", position)
            score = item.get("score", item.get("relevance_score", item.get("relevance", 0.0)))
            try:
                scored.append((int(index), float(score)))
            except (TypeError, ValueError):
                continue
        elif isinstance(item, (int, float)):
            scored.append((position, float(item)))
    valid = [(i, s) for i, s in scored if 0 <= i < count]
    if not valid:
        raise ValueError("Rerank payload contained no usable scores.")
    return sorted(valid, key=lambda pair: pair[1], reverse=True)


class InnKubeReranker(Reranker):
    """Live reranker hitting InnKube's non-OpenAI /rerank route via httpx."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str = "qwen3-reranker-4b",
        timeout: float = 60.0,
    ) -> None:
        """
        Initializes the InnKube reranker client.

        Args:
            api_key: Authentication API key.
            base_url: Endpoint base URL.
            model: Reranker model identifier (e.g. 'qwen3-reranker-4b').
            timeout: HTTP request timeout in seconds.
        """
        self._api_key = api_key or "placeholder_key"
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout

    @property
    def model(self) -> str:
        return self._model

    def rerank(self, query: str, documents: List[str]) -> List[Tuple[int, float]]:
        """POSTs {model, query, documents}; wraps failures in RuntimeError."""
        if not documents:
            return []
        try:
            response = httpx.post(
                f"{self._base_url}/rerank",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"model": self._model, "query": query, "documents": documents},
                timeout=self._timeout,
            )
            response.raise_for_status()
            return parse_rerank_response(response.json(), len(documents))
        except Exception as e:
            logger.error(f"Rerank request failed for model '{self._model}': {e}")
            raise RuntimeError(f"Rerank request failed: {e}") from e


class MockReranker(Reranker):
    """Deterministic reranker for offline tests (never touches the network)."""

    def __init__(self, scores: Optional[Dict[str, float]] = None, model: str = "mock-reranker") -> None:
        self._scores = dict(scores or {})
        self._model = model
        self.calls: List[Tuple[str, List[str]]] = []

    @property
    def model(self) -> str:
        return self._model

    def rerank(self, query: str, documents: List[str]) -> List[Tuple[int, float]]:
        """Scores by scripted per-document values (0.0 default); records calls."""
        self.calls.append((query, list(documents)))
        scored = [(i, float(self._scores.get(doc, 0.0))) for i, doc in enumerate(documents)]
        return sorted(scored, key=lambda pair: pair[1], reverse=True)

