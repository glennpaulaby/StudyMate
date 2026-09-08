import importlib.resources
import logging
import math
import sqlite3
import struct
from abc import ABC, abstractmethod
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def load_sqlite_vector(conn: sqlite3.Connection) -> bool:
    """Loads the sqlite-vector extension into a connection.

    Returns True on success, False on any failure (missing package,
    disabled extension loading, platform mismatch) — callers fall back
    to LIKE-only recall. Never raises.
    """
    try:
        ext_path = importlib.resources.files("sqlite_vector.binaries") / "vector"
        conn.enable_load_extension(True)
        try:
            conn.load_extension(str(ext_path))
        finally:
            conn.enable_load_extension(False)
        return True
    except Exception as e:
        logger.warning(f"sqlite-vector unavailable, vector recall disabled: {e}")
        return False


VECTOR_TYPE = "FLOAT32"

VECTOR_DIM_FALLBACK = 0  # 0 = unknown until the first live vector is seen


def pack_embedding(vector: List[float]) -> bytes:
    """Packs a float vector into a BLOB (little-endian Float32, sqlite-vector format)."""
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack_embedding(blob: bytes, fmt: str = "f") -> List[float]:
    """Unpacks a BLOB back into a float vector.

    `fmt="f"` reads current Float32 rows; `fmt="d"` reads legacy Float64 rows
    (pre-sqlite-vector) for one-way migration.
    """
    size = struct.calcsize(fmt)
    if not blob or len(blob) % size != 0:
        raise ValueError("Corrupt embedding blob.")
    return list(struct.unpack(f"<{len(blob) // size}{fmt}", blob))


def embedding_dim(blob: bytes) -> int:
    """Returns the dimension of a Float32 embedding BLOB."""
    if not blob or len(blob) % 4 != 0:
        raise ValueError("Corrupt embedding blob.")
    return len(blob) // 4


def cosine_similarity(a: List[float], b: List[float]) -> float:
    """Cosine similarity in [-1, 1]; 0.0 when either vector is zero-length/norm."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot / (norm_a * norm_b)))


class Embedder(ABC):
    """Abstract text-embedding provider (real or mock)."""

    @property
    @abstractmethod
    def model(self) -> str:
        """Model identifier; recorded per row to version the embedding space."""
        pass

    @abstractmethod
    def embed(self, texts: List[str]) -> List[List[float]]:
        """Embeds a batch of texts; returns one vector per input, in order."""
        pass


class InnKubeEmbedder(Embedder):
    """Live embedder hitting the InnKube OpenAI-compatible embeddings route."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str = "octen-embedding-8b",
        timeout: float = 60.0,
    ) -> None:
        self._api_key = api_key or "placeholder_key"
        self._base_url = base_url
        self._model = model
        self._timeout = timeout

    @property
    def model(self) -> str:
        return self._model

    def embed(self, texts: List[str]) -> List[List[float]]:
        """Calls client.embeddings.create; wraps transport errors in RuntimeError."""
        if not texts:
            return []
        try:
            from openai import OpenAI, OpenAIError

            client = OpenAI(api_key=self._api_key, base_url=self._base_url, timeout=self._timeout)
            response = client.embeddings.create(model=self._model, input=texts)
            return [list(item.embedding) for item in response.data]
        except Exception as e:
            logger.error(f"Embedding request failed for model '{self._model}': {e}")
            try:
                from openai import OpenAIError as _OE

                if isinstance(e, _OE):
                    raise RuntimeError(f"Embedding API error: {e}") from e
            except ImportError:
                pass
            raise RuntimeError(f"Embedding request failed: {e}") from e


class MockEmbedder(Embedder):
    """Deterministic embedder for offline tests (never touches the network)."""

    def __init__(
        self,
        vectors: Optional[Dict[str, List[float]]] = None,
        dim: int = 4,
        model: str = "mock-embedder",
    ) -> None:
        self._vectors = dict(vectors or {})
        self._dim = dim
        self._model = model
        self.calls: List[List[str]] = []

    @property
    def model(self) -> str:
        return self._model

    def embed(self, texts: List[str]) -> List[List[float]]:
        """Returns scripted vectors (zero vector fallback); records calls."""
        self.calls.append(list(texts))
        return [list(self._vectors.get(t, [0.0] * self._dim)) for t in texts]
