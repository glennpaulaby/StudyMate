import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
from pydantic import BaseModel, Field

from study_agent.core.types import ChatMessage, Role, ToolCall

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    """Returns current UTC timestamp as ISO string."""
    return datetime.now(timezone.utc).isoformat()


class ThreadRecord(BaseModel):
    """Persistent thread metadata (episodic container)."""
    id: str = Field(description="Unique thread identifier")
    name: str = Field(description="Human-readable thread name")
    created_at: str = Field(description="ISO creation timestamp")
    updated_at: str = Field(description="ISO last-activity timestamp")
    archived: bool = Field(default=False, description="Tombstone flag; archived threads are hidden, never deleted")


class FactRecord(BaseModel):
    """Persistent semantic fact (user preference or learned fact)."""
    id: int = Field(description="Auto-increment fact identifier")
    topic: str = Field(description="Short topic label (e.g. 'dijkstra', 'study-preferences')")
    fact: str = Field(description="The durable fact text")
    tags: List[str] = Field(default_factory=list, description="Search tags")
    source_thread: Optional[str] = Field(default=None, description="Thread id where the fact emerged")
    created_at: str = Field(description="ISO creation timestamp")
    archived: bool = Field(default=False, description="Tombstone flag; archived facts are hidden, never deleted")


class MemoryStore:
    """
    SQLite-backed episodic store for chat threads.

    Lives under `workspace/artifacts/memory/` so it stays inside the
    existing `allowed_write` jail. Append-only; no delete API
    (preserves `allow_delete=False` invariant).
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._init_db()
        # Extension load is per-connection; failure degrades to LIKE-only recall.
        from study_agent.memory.embeddings import load_sqlite_vector

        self.vector_available: bool = load_sqlite_vector(self._conn)
        self._vector_inited_for: Optional[str] = None

    def _ensure_vector_init(self, model: str) -> bool:
        """Runs vector_init for the active embedding model; False when unusable.

        The dimension is read from vector_meta (recorded at store time), so a
        model rotation never misreads old vectors — stale rows are simply
        invisible to the vector query (LIKE still sees them).
        """
        if not self.vector_available:
            return False
        if self._vector_inited_for == model:
            return True
        row = self._conn.execute(
            "SELECT dim FROM vector_meta WHERE model = ?", (model,)
        ).fetchone()
        if row is None:
            return False
        try:
            self._conn.execute(
                "SELECT vector_init('facts', 'embedding', ?)",
                (f"type=FLOAT32,dimension={int(row['dim'])},distance=COSINE",),
            )
        except Exception as e:
            logger.warning(f"vector_init failed, vector recall disabled: {e}")
            self.vector_available = False
            return False
        self._vector_inited_for = model
        return True

    def _table_columns(self, table: str) -> List[str]:
        """Returns existing column names for a table (for migrations)."""
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        return [r["name"] for r in rows]

    def _init_db(self) -> None:
        """Creates threads/messages tables if missing; migrates legacy DBs.

        NOTE: explicit BEGIN is required, not just `with`. In sqlite3's
        default legacy isolation mode, implicit transactions open only
        before INSERT/UPDATE/DELETE/REPLACE — DDL (CREATE/ALTER) runs in
        autocommit. Without BEGIN, a failed 2nd CREATE would leave the
        1st committed (corrupt half-schema); proven by
        test_init_db_is_all_or_nothing.
        """
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS threads (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    archived INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL REFERENCES threads(id),
                    role TEXT NOT NULL,
                    content TEXT,
                    tool_calls_json TEXT,
                    tool_call_id TEXT,
                    name TEXT,
                    created_at TEXT NOT NULL,
                    archived INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic TEXT NOT NULL,
                    fact TEXT NOT NULL,
                    tags TEXT NOT NULL DEFAULT '',
                    source_thread TEXT,
                    created_at TEXT NOT NULL,
                    archived INTEGER NOT NULL DEFAULT 0,
                    embedding BLOB,
                    embedding_model TEXT,
                    UNIQUE(topic, fact)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS vector_meta (
                    model TEXT PRIMARY KEY,
                    dim INTEGER NOT NULL
                )
                """
            )
            # Migrate pre-vector DBs (CREATE TABLE IF NOT EXISTS won't alter them).
            if "embedding" not in self._table_columns("facts"):
                self._conn.execute("ALTER TABLE facts ADD COLUMN embedding BLOB")
            if "embedding_model" not in self._table_columns("facts"):
                self._conn.execute("ALTER TABLE facts ADD COLUMN embedding_model TEXT")
            # Migrate pre-archive DBs (CREATE TABLE IF NOT EXISTS won't alter them).
            if "archived" not in self._table_columns("threads"):
                self._conn.execute("ALTER TABLE threads ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
            if "archived" not in self._table_columns("messages"):
                self._conn.execute("ALTER TABLE messages ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id, id)"
            )

    @staticmethod
    def _row_to_thread(row: sqlite3.Row) -> ThreadRecord:
        """Converts a DB row (INTEGER 0/1 archived) to a ThreadRecord (bool)."""
        data = dict(row)
        data["archived"] = bool(data.get("archived", 0))
        return ThreadRecord(**data)

    def create_thread(self, name: str) -> ThreadRecord:
        """Creates a new thread and returns its record."""
        thread_id = uuid.uuid4().hex[:12]
        now = _utc_now_iso()
        with self._conn:
            self._conn.execute(
                "INSERT INTO threads (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (thread_id, name, now, now),
            )
        logger.debug(f"Created thread {thread_id} ({name})")
        return ThreadRecord(id=thread_id, name=name, created_at=now, updated_at=now)

    def list_threads(
        self, include_archived: bool = False, limit: int = 10, offset: int = 0
    ) -> List[ThreadRecord]:
        """Returns one page of threads, most recent first (paginated).

        Every read filters `archived = 0` unless include_archived.
        """
        if include_archived:
            rows = self._conn.execute(
                "SELECT id, name, created_at, updated_at, archived FROM threads "
                "ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, name, created_at, updated_at, archived FROM threads "
                "WHERE archived = 0 ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [self._row_to_thread(r) for r in rows]

    def count_threads(self, include_archived: bool = False) -> int:
        """Returns total thread count (for 'showing X of Y' pagination)."""
        if include_archived:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM threads").fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM threads WHERE archived = 0"
            ).fetchone()
        return int(row["n"]) if row else 0

    def find_threads(self, query: str, limit: int = 10) -> List[ThreadRecord]:
        """Paged lookup by exact id or case-insensitive name substring (non-archived)."""
        q = (query or "").strip()
        if not q:
            return []
        rows = self._conn.execute(
            "SELECT id, name, created_at, updated_at, archived FROM threads "
            "WHERE archived = 0 AND (id = ? OR lower(name) LIKE ?) "
            "ORDER BY updated_at DESC LIMIT ?",
            (q, f"%{q.lower()}%", limit),
        ).fetchall()
        return [self._row_to_thread(r) for r in rows]

    def get_thread(self, thread_id: str, include_archived: bool = False) -> Optional[ThreadRecord]:
        """Returns a single non-archived thread by id, or None if missing/archived."""
        if include_archived:
            row = self._conn.execute(
                "SELECT id, name, created_at, updated_at, archived FROM threads WHERE id = ?",
                (thread_id,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT id, name, created_at, updated_at, archived FROM threads "
                "WHERE id = ? AND archived = 0",
                (thread_id,),
            ).fetchone()
        return self._row_to_thread(row) if row else None

    def rename_thread(self, thread_id: str, name: str) -> bool:
        """Renames a non-archived thread; returns True if a row was updated."""
        with self._conn:
            cur = self._conn.execute(
                "UPDATE threads SET name = ?, updated_at = ? WHERE id = ? AND archived = 0",
                (name, _utc_now_iso(), thread_id),
            )
        return cur.rowcount > 0

    def touch_thread(self, thread_id: str) -> None:
        """Bumps updated_at after new activity (non-archived only)."""
        with self._conn:
            self._conn.execute(
                "UPDATE threads SET updated_at = ? WHERE id = ? AND archived = 0",
                (_utc_now_iso(), thread_id),
            )

    def add_message(self, thread_id: str, message: ChatMessage) -> int:
        """Persists one ChatMessage (incl. tool calls) and returns row id.

        The archived-guard lives *inside* the write transaction: a
        check-then-act outside of it could interleave with a concurrent
        archiver between the SELECT and the INSERT. Raising inside `with`
        rolls the whole unit back.
        """
        tool_calls_json: Optional[str] = None
        if message.tool_calls:
            tool_calls_json = json.dumps([tc.model_dump() for tc in message.tool_calls])
        with self._conn:
            guard = self._conn.execute(
                "SELECT archived FROM threads WHERE id = ?", (thread_id,)
            ).fetchone()
            if guard is None or guard["archived"]:
                raise ValueError(f"Cannot write to missing or archived thread: {thread_id}")
            cur = self._conn.execute(
                """
                INSERT INTO messages
                (thread_id, role, content, tool_calls_json, tool_call_id, name, created_at, archived)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    thread_id,
                    message.role.value,
                    message.content,
                    tool_calls_json,
                    message.tool_call_id,
                    message.name,
                    _utc_now_iso(),
                ),
            )
            self._conn.execute(
                "UPDATE threads SET updated_at = ? WHERE id = ? AND archived = 0",
                (_utc_now_iso(), thread_id),
            )
        return int(cur.lastrowid)

    def archive_thread(self, thread_id: str) -> bool:
        """Tombstones a thread and all its messages (no deletes). Returns True if found."""
        with self._conn:
            cur = self._conn.execute(
                "UPDATE threads SET archived = 1, updated_at = ? WHERE id = ? AND archived = 0",
                (_utc_now_iso(), thread_id),
            )
            self._conn.execute(
                "UPDATE messages SET archived = 1 WHERE thread_id = ?", (thread_id,)
            )
        return cur.rowcount > 0

    def archive_all(self) -> int:
        """Tombstones every non-archived thread + message. Returns thread count."""
        with self._conn:
            cur = self._conn.execute(
                "UPDATE threads SET archived = 1, updated_at = ? WHERE archived = 0",
                (_utc_now_iso(),),
            )
            self._conn.execute("UPDATE messages SET archived = 1 WHERE archived = 0")
        return cur.rowcount

    def get_messages(self, thread_id: str, limit: int = 50, offset: int = 0) -> List[ChatMessage]:
        """Returns one page of non-archived messages, oldest first within the page.

        Page 0 (`offset=0`) is the most recent `limit` messages; higher offsets
        go further back. Every read filters `archived = 0`.
        """
        rows = self._conn.execute(
            """
            SELECT role, content, tool_calls_json, tool_call_id, name
            FROM messages WHERE thread_id = ? AND archived = 0 ORDER BY id DESC LIMIT ? OFFSET ?
            """,
            (thread_id, limit, offset),
        ).fetchall()
        messages: List[ChatMessage] = []
        for row in reversed(rows):
            tool_calls: Optional[List[ToolCall]] = None
            if row["tool_calls_json"]:
                try:
                    raw = json.loads(row["tool_calls_json"])
                    tool_calls = [ToolCall(**tc) for tc in raw]
                except (json.JSONDecodeError, TypeError, ValueError) as e:
                    logger.warning(f"Skipping corrupt tool_calls payload: {e}")
            messages.append(
                ChatMessage(
                    role=Role(row["role"]),
                    content=row["content"],
                    tool_calls=tool_calls,
                    tool_call_id=row["tool_call_id"],
                    name=row["name"],
                )
            )
        return messages

    def count_messages(self, thread_id: str) -> int:
        """Returns number of non-archived messages for a thread."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE thread_id = ? AND archived = 0",
            (thread_id,),
        ).fetchone()
        return int(row["n"]) if row else 0

    @staticmethod
    def _row_to_fact(row: sqlite3.Row) -> FactRecord:
        """Converts a DB row (comma tags, INTEGER archived) to a FactRecord."""
        data = dict(row)
        data["tags"] = [t for t in (data.get("tags") or "").split(",") if t]
        data["archived"] = bool(data.get("archived", 0))
        return FactRecord(**data)

    def store_fact(
        self,
        topic: str,
        fact: str,
        tags: Optional[List[str]] = None,
        source_thread: Optional[str] = None,
        embedding: Optional[bytes] = None,
        embedding_model: Optional[str] = None,
    ) -> tuple[FactRecord, bool]:
        """Stores a fact; exact (topic, fact) duplicates revive instead of duplicating.

        Returns (record, created). Whole unit (lookup + insert-or-revive) runs
        in one transaction; UNIQUE(topic, fact) is the backstop against races.
        Pass embedding (+model) to make the row visible to vector recall;
        rows without one stay LIKE-only. Duplicate stores fill in a missing
        embedding and record the vector space in vector_meta.
        """
        topic = (topic or "").strip()
        fact = (fact or "").strip()
        if not topic or not fact:
            raise ValueError("Both topic and fact must be non-empty.")
        if len(fact) > 2000:
            raise ValueError("Fact text exceeds 2000 characters; shorten it.")
        if (embedding is None) != (embedding_model is None):
            raise ValueError("embedding and embedding_model must be given together.")
        tags_str = ",".join(t.strip().lower() for t in (tags or []) if t.strip())
        with self._conn:
            if embedding is not None:
                from study_agent.memory.embeddings import embedding_dim

                self._conn.execute(
                    "INSERT INTO vector_meta (model, dim) VALUES (?, ?) "
                    "ON CONFLICT(model) DO UPDATE SET dim = excluded.dim",
                    (embedding_model, embedding_dim(embedding)),
                )
            try:
                cur = self._conn.execute(
                    "INSERT INTO facts (topic, fact, tags, source_thread, created_at, archived, "
                    "embedding, embedding_model) VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                    (topic, fact, tags_str, source_thread, _utc_now_iso(), embedding, embedding_model),
                )
                row = self._conn.execute(
                    "SELECT id, topic, fact, tags, source_thread, created_at, archived "
                    "FROM facts WHERE id = ?",
                    (cur.lastrowid,),
                ).fetchone()
                return self._row_to_fact(row), True
            except sqlite3.IntegrityError:
                row = self._conn.execute(
                    "SELECT id, topic, fact, tags, source_thread, created_at, archived "
                    "FROM facts WHERE topic = ? AND fact = ?",
                    (topic, fact),
                ).fetchone()
                if row is None:  # pragma: no cover - defensive; UNIQUE guarantees a row
                    raise
                if row["archived"]:
                    self._conn.execute("UPDATE facts SET archived = 0 WHERE id = ?", (row["id"],))
                full = self._conn.execute(
                    "SELECT id, topic, fact, tags, source_thread, created_at, archived, "
                    "embedding, embedding_model FROM facts WHERE id = ?",
                    (row["id"],),
                ).fetchone()
                if embedding is not None and (
                    full["embedding"] is None or full["embedding_model"] != embedding_model
                ):
                    self._conn.execute(
                        "UPDATE facts SET embedding = ?, embedding_model = ? WHERE id = ?",
                        (embedding, embedding_model, row["id"]),
                    )
                row = self._conn.execute(
                    "SELECT id, topic, fact, tags, source_thread, created_at, archived "
                    "FROM facts WHERE id = ?",
                    (row["id"],),
                ).fetchone()
                return self._row_to_fact(row), False

    def recall_facts(self, query: str = "", limit: int = 10, offset: int = 0) -> List[FactRecord]:
        """Returns one page of non-archived facts, most recent first.

        Empty query lists recent facts; otherwise case-insensitive substring
        match over topic/fact/tags (same literal semantics as search_text).
        """
        q = (query or "").strip().lower()
        if not q:
            rows = self._conn.execute(
                "SELECT id, topic, fact, tags, source_thread, created_at, archived "
                "FROM facts WHERE archived = 0 ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        else:
            like = f"%{q}%"
            rows = self._conn.execute(
                "SELECT id, topic, fact, tags, source_thread, created_at, archived "
                "FROM facts WHERE archived = 0 AND (lower(topic) LIKE ? OR lower(fact) LIKE ? "
                "OR lower(tags) LIKE ?) ORDER BY id DESC LIMIT ? OFFSET ?",
                (like, like, like, limit, offset),
            ).fetchall()
        return [self._row_to_fact(r) for r in rows]

    def count_facts(self, query: str = "") -> int:
        """Returns total non-archived fact count (for paging), optionally filtered."""
        q = (query or "").strip().lower()
        if not q:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM facts WHERE archived = 0").fetchone()
        else:
            like = f"%{q}%"
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM facts WHERE archived = 0 AND (lower(topic) LIKE ? "
                "OR lower(fact) LIKE ? OR lower(tags) LIKE ?)",
                (like, like, like),
            ).fetchone()
        return int(row["n"]) if row else 0

    def get_fact(self, fact_id: int, include_archived: bool = False) -> Optional[FactRecord]:
        """Returns a single fact by id, or None if missing/archived."""
        if include_archived:
            row = self._conn.execute(
                "SELECT id, topic, fact, tags, source_thread, created_at, archived "
                "FROM facts WHERE id = ?",
                (fact_id,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT id, topic, fact, tags, source_thread, created_at, archived "
                "FROM facts WHERE id = ? AND archived = 0",
                (fact_id,),
            ).fetchone()
        return self._row_to_fact(row) if row else None

    def archive_fact(self, fact_id: int) -> bool:
        """Tombstones a fact (no deletes). Returns True if a row was hidden."""
        with self._conn:
            cur = self._conn.execute(
                "UPDATE facts SET archived = 1 WHERE id = ? AND archived = 0", (fact_id,)
            )
        return cur.rowcount > 0

    def recall_semantic_vector(
        self, query_blob: bytes, model: str, limit: int = 10, offset: int = 0
    ) -> List[tuple]:
        """Vector nearest-neighbors via sqlite-vector (exact scan, cosine).

        Returns [(FactRecord, distance)] ascending by distance. Only rows
        embedded with `model` participate (stale spaces stay invisible here
        and remain LIKE-visible). Empty list when the extension is missing,
        uninitialized, or nothing is embedded yet — callers fall back to LIKE.
        """
        if not self._ensure_vector_init(model):
            return []
        k = max(1, limit + offset)
        try:
            rows = self._conn.execute(
                "SELECT f.id, f.topic, f.fact, f.tags, f.source_thread, f.created_at, "
                "f.archived, v.distance FROM vector_full_scan('facts', 'embedding', ?, ?) AS v "
                "JOIN facts AS f ON f.rowid = v.rowid "
                "WHERE f.archived = 0 AND f.embedding_model = ?",
                (query_blob, k, model),
            ).fetchall()
        except Exception as e:
            logger.warning(f"Vector scan failed, falling back to LIKE: {e}")
            return []
        page = rows[offset : offset + limit]
        return [(self._row_to_fact(r), float(r["distance"])) for r in page]

    def get_facts_missing_embeddings(self, model: str, limit: int = 100) -> List[FactRecord]:
        """Returns non-archived facts lacking a vector for `model` (backfill queue)."""
        rows = self._conn.execute(
            "SELECT id, topic, fact, tags, source_thread, created_at, archived "
            "FROM facts WHERE archived = 0 AND (embedding IS NULL OR embedding_model IS NULL "
            "OR embedding_model != ?) ORDER BY id ASC LIMIT ?",
            (model, limit),
        ).fetchall()
        return [self._row_to_fact(r) for r in rows]

    def set_fact_embedding(self, fact_id: int, embedding: bytes, model: str) -> bool:
        """Attaches a vector to a fact; records the vector space in vector_meta."""
        from study_agent.memory.embeddings import embedding_dim

        with self._conn:
            self._conn.execute(
                "INSERT INTO vector_meta (model, dim) VALUES (?, ?) "
                "ON CONFLICT(model) DO UPDATE SET dim = excluded.dim",
                (model, embedding_dim(embedding)),
            )
            cur = self._conn.execute(
                "UPDATE facts SET embedding = ?, embedding_model = ? "
                "WHERE id = ? AND archived = 0",
                (embedding, model, fact_id),
            )
        return cur.rowcount > 0

    def close(self) -> None:
        """Closes the underlying SQLite connection."""
        try:
            self._conn.close()
        except Exception as e:
            logger.warning(f"Error closing memory DB: {e}")
