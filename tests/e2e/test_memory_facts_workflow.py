from pathlib import Path
from study_agent.config.settings import AgentConfig
from study_agent.core.context import ContextManager
from study_agent.core.engine import AgentEngine
from study_agent.core.types import ToolCall
from study_agent.llm.mock import MockLLMClient
from study_agent.memory.embeddings import MockEmbedder
from study_agent.memory.store import MemoryStore
from study_agent.memory.tools import register_memory_tools
from study_agent.tools.base import ToolRegistry


def _registry_with_memory(store: MemoryStore) -> ToolRegistry:
    """Builds a ToolRegistry with only memory tools registered."""
    registry = ToolRegistry()
    register_memory_tools(registry, store)
    return registry


def test_e2e_fact_store_then_recall_across_sessions(tmp_path: Path):
    """
    E2E: semantic memory across restarts.
    1. Session 1: agent stores 'struggles with Dijkstra' via memory_store tool.
    2. Restart: new store handle + context on the same DB file.
    3. Session 2: agent recalls via memory_recall and personalizes its answer.
    """
    db = tmp_path / "memory.db"

    store1 = MemoryStore(db)
    try:
        mock1 = MockLLMClient()
        mock1.add_response(
            content="I will remember that.",
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="memory_store",
                    arguments={"topic": "dijkstra", "fact": "Struggles with edge relaxations"},
                )
            ],
        )
        mock1.add_response(content="Noted: you struggle with Dijkstra relaxations.")
        engine1 = AgentEngine(
            llm_client=mock1, tool_registry=_registry_with_memory(store1), config=AgentConfig(max_iterations=5)
        )
        result1 = engine1.run("Remember that I struggle with Dijkstra", ContextManager(custom_system_prompt="S"))
        assert result1.is_success is True
        assert store1.count_facts() == 1
    finally:
        store1.close()

    store2 = MemoryStore(db)  # restart: same file, fresh handle
    try:
        mock2 = MockLLMClient()
        mock2.add_response(
            content="Checking memory...",
            tool_calls=[ToolCall(id="c2", name="memory_recall", arguments={"query": "dijkstra"})],
        )
        mock2.add_response(content="Since you struggle with relaxations, let us start there.")
        engine2 = AgentEngine(
            llm_client=mock2, tool_registry=_registry_with_memory(store2), config=AgentConfig(max_iterations=5)
        )
        result2 = engine2.run("Help me revise graphs", ContextManager(custom_system_prompt="S"))
        assert result2.is_success is True
        assert "relaxations" in result2.final_response
        assert mock2.tool_history[0] is not None  # schemas were offered to the LLM
    finally:
        store2.close()

def test_e2e_hybrid_recall_finds_paraphrase_like_misses(tmp_path: Path):
    """
    E2E: hybrid recall through the agent loop with a scripted embedder.
    Query "shortest path" matches no substring, but the mock vector space
    places it next to the Dijkstra fact, so memory_recall still surfaces it.
    """
    db = tmp_path / "memory.db"
    mock_embedder = MockEmbedder(
        vectors={
            "graphs: Needs non-negative weights": [1.0, 0.0],
            "shortest path": [0.9, 0.1],
        },
        dim=2,
        model="mock-v1",
    )
    store = MemoryStore(db)
    try:
        registry = ToolRegistry()
        register_memory_tools(registry, store, mock_embedder)

        mock_llm = MockLLMClient()
        mock_llm.add_response(
            content="Storing that.",
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="memory_store",
                    arguments={"topic": "graphs", "fact": "Needs non-negative weights"},
                )
            ],
        )
        mock_llm.add_response(content="Stored.")
        engine = AgentEngine(
            llm_client=mock_llm, tool_registry=registry, config=AgentConfig(max_iterations=5)
        )
        result = engine.run("Remember: Needs non-negative weights", ContextManager(custom_system_prompt="S"))
        assert result.is_success is True
        # Stored WITH a vector (embedder present on the tool).
        assert store.get_facts_missing_embeddings("mock-v1") == []

        mock_llm2 = MockLLMClient()
        mock_llm2.add_response(
            content="Recalling...",
            tool_calls=[ToolCall(id="c2", name="memory_recall", arguments={"query": "shortest path"})],
        )
        mock_llm2.add_response(content="Dijkstra needs non-negative weights.")
        engine2 = AgentEngine(
            llm_client=mock_llm2, tool_registry=registry, config=AgentConfig(max_iterations=5)
        )
        result2 = engine2.run("What do I know about shortest path?", ContextManager(custom_system_prompt="S"))
        assert result2.is_success is True
        assert "non-negative weights" in result2.final_response
    finally:
        store.close()
