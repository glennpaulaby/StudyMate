from pathlib import Path
from study_agent.config.settings import AgentConfig
from study_agent.core.context import ContextManager
from study_agent.core.engine import AgentEngine
from study_agent.core.types import ToolCall
from study_agent.llm.mock import MockLLMClient
from study_agent.memory.store import MemoryStore
from study_agent.memory.threads import generate_thread_name
from study_agent.tools.base import ToolRegistry


def test_e2e_thread_persist_and_resume(tmp_path: Path):
    """
    E2E: chat survivability across restart.
    1. Session 1: engine answers in a thread, persist context to SQLite.
    2. Restart: new store + context loads thread history.
    3. Session 2: agent continues with prior context available.
    """
    db = tmp_path / "memory.db"
    store = MemoryStore(db)
    try:
        thread = store.create_thread(generate_thread_name("Help me with Dijkstra"))
        ctx1 = ContextManager(custom_system_prompt="System")
        mock1 = MockLLMClient()
        mock1.add_response(content="Dijkstra needs non-negative weights.")
        engine1 = AgentEngine(llm_client=mock1, tool_registry=ToolRegistry(), config=AgentConfig(max_iterations=5))
        pre_len = len(ctx1.get_messages())
        result1 = engine1.run("Help me with Dijkstra", ctx1)
        assert result1.is_success is True
        for msg in ctx1.get_messages()[pre_len:]:
            store.add_message(thread.id, msg)

        # Restart: fresh objects, same DB file
        ctx2 = ContextManager(custom_system_prompt="System")
        ctx2.load_history(store.get_messages(thread.id))
        assert ctx2.user_turn_count == 1

        mock2 = MockLLMClient()
        mock2.add_response(
            content="Continuing?",
            tool_calls=[ToolCall(id="c1", name="missing_tool", arguments={})],
        )
        mock2.add_response(content="Resumed with prior Dijkstra context.")
        engine2 = AgentEngine(llm_client=mock2, tool_registry=ToolRegistry(), config=AgentConfig(max_iterations=5))
        result2 = engine2.run("Continue where we left off", ctx2)
        assert result2.is_success is True
        assert "Dijkstra" in result2.final_response or "Resumed" in result2.final_response
    finally:
        store.close()
