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

    def close(self) -> None:
        """Closes the underlying SQLite connection."""
        try:
            self._conn.close()
        except Exception as e:
            logger.warning(f"Error closing memory DB: {e}")
