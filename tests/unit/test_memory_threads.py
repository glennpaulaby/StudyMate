import sqlite3
from pathlib import Path
from study_agent.core.context import ContextManager
from study_agent.core.types import ChatMessage, Role, ToolCall
from study_agent.memory.store import MemoryStore
from study_agent.memory.threads import generate_thread_name, resolve_thread


def test_thread_create_and_list(tmp_path: Path):
    """Verify threads are created and listed most-recent-first."""
    store = MemoryStore(tmp_path / "memory.db")
    t1 = store.create_thread("dijkstra-help")
    t2 = store.create_thread("bellman-ford")
    try:
        threads = store.list_threads()
        assert len(threads) == 2
        assert threads[0].id == t2.id  # most recent first
        assert store.get_thread(t1.id).name == "dijkstra-help"
    finally:
        store.close()


def test_messages_roundtrip_with_tool_calls(tmp_path: Path):
    """Verify full ChatMessage (incl. tool calls) survives SQLite roundtrip."""
    store = MemoryStore(tmp_path / "memory.db")
    try:
        thread = store.create_thread("react-replay")
        store.add_message(thread.id, ChatMessage(role=Role.USER, content="Explain Dijkstra"))
        store.add_message(
            thread.id,
            ChatMessage(
                role=Role.ASSISTANT,
                content=None,
                tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "a.txt"})],
            ),
        )
        store.add_message(
            thread.id,
            ChatMessage(role=Role.TOOL, content="file contents", tool_call_id="c1", name="read_file"),
        )
        loaded = store.get_messages(thread.id)
        assert len(loaded) == 3
        assert loaded[0].role == Role.USER
        assert loaded[1].tool_calls[0].name == "read_file"
        assert loaded[2].role == Role.TOOL
        assert store.count_messages(thread.id) == 3
    finally:
        store.close()


def test_persistence_across_reopen_restart_sim(tmp_path: Path):
    """Verify session N data is retrievable in session N+1 (restart)."""
    db = tmp_path / "memory.db"
    store1 = MemoryStore(db)
    thread_id = store1.create_thread("grill-session").id
    store1.add_message(thread_id, ChatMessage(role=Role.USER, content="Remember I struggle with Dijkstra"))
    store1.close()

    store2 = MemoryStore(db)  # new process / restart
    try:
        assert store2.get_thread(thread_id) is not None
        msgs = store2.get_messages(thread_id)
        assert len(msgs) == 1
        assert "Dijkstra" in (msgs[0].content or "")
    finally:
        store2.close()


def test_generate_and_resolve_thread_name():
    """Verify heuristic naming and id/name resolution."""
    assert generate_thread_name("  Explain Dijkstra Algorithm!! ") == "explain-dijkstra-algorithm"
    assert generate_thread_name("").startswith("thread-")
    assert generate_thread_name("Hi", max_len=2) == "hi"

    store_path = "unused"
    _ = store_path
    from study_agent.memory.store import ThreadRecord

    threads = [
        ThreadRecord(id="abc123def456", name="dijkstra-help", created_at="t", updated_at="t"),
        ThreadRecord(id="zzz999000111", name="grill", created_at="t", updated_at="t"),
    ]
    assert resolve_thread(threads, "abc123def456").name == "dijkstra-help"
    assert resolve_thread(threads, "abc123").name == "dijkstra-help"  # id prefix
    assert resolve_thread(threads, "GRILL").id == "zzz999000111"  # case-insensitive name
    assert resolve_thread(threads, "missing") is None
    assert resolve_thread(threads, "") is None


def test_archive_this_hides_thread_and_messages(tmp_path: Path):
    """Verify /archive-this tombstones one thread; reads filter archived rows."""
    store = MemoryStore(tmp_path / "memory.db")
    try:
        keep = store.create_thread("keep-me")
        drop = store.create_thread("drop-me")
        store.add_message(keep.id, ChatMessage(role=Role.USER, content="keep"))
        store.add_message(drop.id, ChatMessage(role=Role.USER, content="drop"))

        assert store.archive_thread(drop.id) is True
        assert store.archive_thread(drop.id) is False  # already archived

        visible = [t.id for t in store.list_threads()]
        assert keep.id in visible and drop.id not in visible
        assert store.get_thread(drop.id) is None
        assert store.get_thread(drop.id, include_archived=True) is not None
        assert store.get_messages(drop.id) == []
        assert store.count_messages(drop.id) == 0
        # Archived thread rejects new writes
        try:
            store.add_message(drop.id, ChatMessage(role=Role.USER, content="late"))
            raise AssertionError("expected ValueError for archived thread")
        except ValueError:
            pass
    finally:
        store.close()


class _FlakyConn:
    """Connection proxy failing the messages CREATE to prove _init_db atomicity."""

    def __init__(self, real):
        object.__setattr__(self, "_real", real)

    def execute(self, sql, *args, **kwargs):
        if "CREATE TABLE IF NOT EXISTS messages" in sql:
            raise sqlite3.OperationalError("simulated failure")
        return self._real.execute(sql, *args, **kwargs)

    def __enter__(self):
        self._real.__enter__()
        return self

    def __exit__(self, *exc):
        return self._real.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __setattr__(self, name, value):
        setattr(self._real, name, value)


def test_init_db_is_all_or_nothing(tmp_path: Path, monkeypatch):
    """Fault-injection: if the 2nd CREATE fails, the 1st rolls back (no corrupt half-DB)."""
    db = tmp_path / "fault.db"
    real_connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: _FlakyConn(real_connect(*a, **k)))
    try:
        MemoryStore(db)
        raise AssertionError("expected OperationalError from _init_db")
    except sqlite3.OperationalError:
        pass

    conn = sqlite3.connect(str(db))
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    finally:
        conn.close()
    assert "threads" not in tables and "messages" not in tables

    # Path still usable fresh afterwards.
    monkeypatch.undo()
    store = MemoryStore(db)
    try:
        thread = store.create_thread("after-fault")
        store.add_message(thread.id, ChatMessage(role=Role.USER, content="clean"))
        assert store.count_messages(thread.id) == 1
    finally:
        store.close()


def test_failed_write_rolls_back_cleanly(tmp_path: Path):
    """Verify a rejected add_message leaves no partial rows (guard inside txn)."""
    store = MemoryStore(tmp_path / "memory.db")
    try:
        thread = store.create_thread("atomic-check")
        store.add_message(thread.id, ChatMessage(role=Role.USER, content="before"))
        before = store.get_thread(thread.id, include_archived=True)
        try:
            store.add_message("missing-thread", ChatMessage(role=Role.USER, content="ghost"))
            raise AssertionError("expected ValueError for missing thread")
        except ValueError:
            pass
        # No partial message row, thread row untouched.
        total_msgs = store._conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
        assert total_msgs == 1
        after = store.get_thread(thread.id, include_archived=True)
        assert after.updated_at == before.updated_at
    finally:
        store.close()


def test_archive_all_hides_everything(tmp_path: Path):
    """Verify /archive-all tombstones all threads and messages."""
    store = MemoryStore(tmp_path / "memory.db")
    try:
        for name in ("t1", "t2"):
            t = store.create_thread(name)
            store.add_message(t.id, ChatMessage(role=Role.USER, content=f"msg {name}"))
        assert store.archive_all() == 2
        assert store.list_threads() == []
        assert len(store.list_threads(include_archived=True)) == 2
    finally:
        store.close()


def test_legacy_db_migration_adds_archived(tmp_path: Path):
    """Verify pre-archive DB files gain the archived column on open."""
    import sqlite3

    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT, tool_calls_json TEXT, tool_call_id TEXT, name TEXT, created_at TEXT NOT NULL)")
    conn.execute("INSERT INTO threads (id, name, created_at, updated_at) VALUES ('abc', 'old', 't', 't')")
    conn.execute("INSERT INTO messages (thread_id, role, content, created_at) VALUES ('abc', 'user', 'hello', 't')")
    conn.commit()
    conn.close()

    store = MemoryStore(db)  # triggers ALTER TABLE migration
    try:
        assert store.get_thread("abc") is not None  # readable with default archived=0
        assert len(store.get_messages("abc")) == 1
        assert store.archive_thread("abc") is True
        assert store.list_threads() == []
    finally:
        store.close()


def test_list_threads_pagination(tmp_path: Path):
    """Verify thread reads are paged (no unbounded full-table fetch)."""
    store = MemoryStore(tmp_path / "memory.db")
    try:
        for i in range(25):
            store.create_thread(f"thread-{i:02d}")
        assert store.count_threads() == 25
        page1 = store.list_threads(limit=10, offset=0)
        page2 = store.list_threads(limit=10, offset=10)
        page3 = store.list_threads(limit=10, offset=20)
        assert len(page1) == 10 and len(page2) == 10 and len(page3) == 5
        ids = [t.id for t in page1 + page2 + page3]
        assert len(set(ids)) == 25  # no overlaps, full coverage across pages
        # Positional pick used by /thread <#>
        assert store.list_threads(limit=1, offset=11)[0].id == page2[1].id
    finally:
        store.close()


def test_get_messages_pagination(tmp_path: Path):
    """Verify message reads are paged oldest-first within each page."""
    store = MemoryStore(tmp_path / "memory.db")
    try:
        thread = store.create_thread("long-thread")
        for i in range(25):
            store.add_message(thread.id, ChatMessage(role=Role.USER, content=f"msg {i}"))
        p1 = store.get_messages(thread.id, limit=10, offset=0)
        p2 = store.get_messages(thread.id, limit=10, offset=10)
        assert [m.content for m in p1] == [f"msg {i}" for i in range(15, 25)]
        assert [m.content for m in p2] == [f"msg {i}" for i in range(5, 15)]
    finally:
        store.close()


def test_find_threads_paged_lookup(tmp_path: Path):
    """Verify /thread name lookup needs no full-table scan."""
    store = MemoryStore(tmp_path / "memory.db")
    try:
        store.create_thread("dijkstra-deep-dive")
        store.create_thread("bellman-ford-basics")
        hits = store.find_threads("dijkstra")
        assert len(hits) == 1 and hits[0].name == "dijkstra-deep-dive"
        assert store.find_threads("no-such-topic") == []
        assert store.find_threads("") == []
    finally:
        store.close()


def test_cancel_sentinel_survives_questionary():
    """Regression: Choice(value=None) defaults to title, so Cancel needs a sentinel."""
    import questionary
    from study_agent.cli.terminal import _CANCEL, _SEE_MORE

    assert _CANCEL != _SEE_MORE
    assert questionary.Choice(title="✕ Cancel", value=_CANCEL).value == _CANCEL


def test_pick_fallback_cancel_invalid_and_valid(tmp_path: Path, monkeypatch):
    """Verify numbered fallback: cancel/invalid -> None, valid # -> ThreadRecord."""
    from study_agent.cli import terminal as term

    store = MemoryStore(tmp_path / "m.db")
    try:
        thread = store.create_thread("fallback-pick")
        monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *a, **k: "")
        assert term.pick_thread_interactive(store) is None
        monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *a, **k: "99")
        assert term.pick_thread_interactive(store) is None
        monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *a, **k: "1")
        chosen = term.pick_thread_interactive(store)
        assert chosen is not None and chosen.id == thread.id  # never a raw str
    finally:
        store.close()


def test_context_load_history_keeps_system_prompt():
    """Verify entering a thread keeps system prompt and restores history."""
    ctx = ContextManager(custom_system_prompt="System instructions")
    history = [
        ChatMessage(role=Role.USER, content="What is QuickSort?"),
        ChatMessage(role=Role.ASSISTANT, content="Divide and conquer."),
    ]
    ctx.load_history(history)
    msgs = ctx.get_messages()
    assert len(msgs) == 3  # system + 2
    assert msgs[0].role == Role.SYSTEM
    assert ctx.user_turn_count == 1
