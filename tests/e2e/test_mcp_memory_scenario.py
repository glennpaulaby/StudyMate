import json
from pathlib import Path
import httpx

from study_agent.config.settings import AgentConfig, FilesystemToolConfig
from study_agent.core.context import ContextManager
from study_agent.core.engine import AgentEngine
from study_agent.core.types import ToolCall
from study_agent.llm.mock import MockLLMClient
from study_agent.memory.embeddings import MockEmbedder
from study_agent.memory.store import MemoryStore
from study_agent.memory.tools import register_memory_tools
from study_agent.study.linter import VaultLinter
from study_agent.study.templates import format_concept_note
from study_agent.tools.base import ToolRegistry
from study_agent.tools.filesystem import register_filesystem_tools
from study_agent.tools.mcp.services import WebResearchService
from study_agent.tools.mcp.tools import register_mcp_tools


class FakeMultiStepSearchClient:
    def get(self, url: str) -> httpx.Response:
        html = """
        <a class="result__a" href="https://algorithms.org/bellman-ford">Bellman-Ford Cycles</a>
        <div class="result__snippet">Bellman-Ford relaxes edges |V|-1 times and detects negative cycles by checking if a further relaxation reduces distance.</div>
        """
        return httpx.Response(
            200,
            text=html,
            headers={"content-type": "text/html"},
            request=httpx.Request("GET", url),
        )


def test_e2e_multistep_mcp_and_memory_scenario(tmp_path: Path):
    """
    E2E Scenario Benchmark:
    Multistep agent flow orchestrating:
      1. Long-Term Memory Recall (retrieving user weakness on Dijkstra/Bellman-Ford)
      2. Custom MCP Knowledge Graph & Vault search/read/links (inspecting dijkstra.md)
      3. External Web Research via search provider (Bellman-Ford negative cycle detection)
      4. Targeted MCP section update & concept note creation (inserting insights & linking)
      5. Long-Term Memory Store (saving newly learned user mastery milestone)
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifacts_dir = workspace / "artifacts"
    artifacts_dir.mkdir()
    concepts_dir = artifacts_dir / "concepts"
    concepts_dir.mkdir()

    # Pre-populate vault with baseline concept note
    dijkstra_md = format_concept_note(
        title="Dijkstra Algorithm",
        course="Algorithms",
        one_line_summary="Single-source shortest path for non-negative edge weights.",
        core_intuition="Greedily visit lowest-cost unvisited vertex.",
        formal_definition="Maintains priority queue of tentative distances.",
        connections=["graphs", "shortest-path"],
    )
    (concepts_dir / "dijkstra.md").write_text(dijkstra_md, encoding="utf-8")

    # Pre-populate memory DB with user's historical struggle
    memory_db_path = artifacts_dir / "memory" / "memory.db"
    memory_db_path.parent.mkdir(parents=True, exist_ok=True)
    store = MemoryStore(memory_db_path)
    embedder = MockEmbedder({"dijkstra struggle": [1.0, 0.0]})
    store.store_fact(
        topic="algorithms-weakness",
        fact="Student struggles with negative edge cycles and why Dijkstra fails on negative weights.",
        tags=["dijkstra", "graphs", "exam"],
    )

    # Initialize ToolRegistry with Core Filesystem + Custom MCP + Memory Tools
    registry = ToolRegistry()
    fs_config = FilesystemToolConfig(
        allowed_read_paths=[str(workspace)],
        allowed_write_paths=[str(artifacts_dir)],
    )
    register_filesystem_tools(registry, base_dir=workspace, config=fs_config)
    register_mcp_tools(
        registry,
        vault_path=artifacts_dir,
        search_service=WebResearchService(client=FakeMultiStepSearchClient()),
    )
    register_memory_tools(registry, store=store, embedder=embedder)

    # Script the MockLLM Multi-Step ReAct Sequence
    mock_llm = MockLLMClient()

    # Step 1: LLM recalls user's past struggle from Long-Term Memory
    mock_llm.add_response(
        content="Recalling user's historical struggles regarding Dijkstra and Bellman-Ford...",
        tool_calls=[
            ToolCall(
                id="call_mem_1",
                name="memory_recall",
                arguments={"query": "dijkstra struggle"},
            )
        ],
    )

    # Step 2: LLM searches the custom Obsidian MCP vault for existing notes
    mock_llm.add_response(
        content="Searching the study vault for existing notes on Dijkstra...",
        tool_calls=[
            ToolCall(
                id="call_mcp_1",
                name="obsidian_search_notes",
                arguments={"query": "Dijkstra"},
            )
        ],
    )

    # Step 3: LLM reads the note content via MCP
    mock_llm.add_response(
        content="Reading concepts/dijkstra.md...",
        tool_calls=[
            ToolCall(
                id="call_mcp_2",
                name="obsidian_read_note",
                arguments={"path": "concepts/dijkstra.md"},
            )
        ],
    )

    # Step 4: LLM queries outgoing links to inspect the knowledge graph
    mock_llm.add_response(
        content="Inspecting knowledge graph connections for Dijkstra...",
        tool_calls=[
            ToolCall(
                id="call_mcp_3",
                name="obsidian_get_outgoing_links",
                arguments={"path": "concepts/dijkstra.md"},
            )
        ],
    )

    # Step 5: LLM executes external web research on Bellman-Ford negative cycles
    mock_llm.add_response(
        content="Researching external academic resources on Bellman-Ford negative cycles...",
        tool_calls=[
            ToolCall(
                id="call_web_1",
                name="research_topic",
                arguments={
                    "topic": "Bellman-Ford negative cycle detection",
                    "max_results": 2,
                    "depth": 0,
                },
            )
        ],
    )

    # Step 6: LLM inserts the researched insight into the specific section of dijkstra.md
    mock_llm.add_response(
        content="Enriching Dijkstra note with negative weight limitations...",
        tool_calls=[
            ToolCall(
                id="call_mcp_4",
                name="obsidian_insert_into_section",
                arguments={
                    "path": "concepts/dijkstra.md",
                    "heading": "Core Intuition",
                    "content": "Note: Dijkstra fails with negative edge weights. Use [[bellman-ford]] for negative weights.",
                },
            )
        ],
    )

    # Step 7: LLM writes a new concept note for negative-cycles
    neg_cycles_content = format_concept_note(
        title="Negative Weight Cycles",
        course="Algorithms",
        one_line_summary="A cycle whose edges sum to a negative value, breaking shortest-path definitions.",
        core_intuition="Infinite reduction of path cost by looping.",
        formal_definition="Bellman-Ford detects this if distance relaxes on the |V|-th iteration.",
        connections=["dijkstra", "bellman-ford"],
    )
    mock_llm.add_response(
        content="Creating new concept note for Negative Cycles...",
        tool_calls=[
            ToolCall(
                id="call_mcp_5",
                name="obsidian_write_note",
                arguments={
                    "path": "concepts/negative-cycles.md",
                    "content": neg_cycles_content,
                },
            )
        ],
    )

    # Step 8: LLM stores the updated mastery milestone in Long-Term Memory
    mock_llm.add_response(
        content="Recording updated study progress into long term memory...",
        tool_calls=[
            ToolCall(
                id="call_mem_2",
                name="memory_store",
                arguments={
                    "topic": "algorithms-exam",
                    "fact": "Mastered negative cycle detection differences between Dijkstra and Bellman-Ford.",
                    "tags": "graphs,exam,dijkstra",
                },
            )
        ],
    )

    # Step 9: Final synthesized response to student
    mock_llm.add_response(
        content=(
            "I have retrieved your past difficulty regarding Dijkstra with negative weights, "
            "verified your local vault notes, researched Bellman-Ford cycle detection, "
            "updated 'concepts/dijkstra.md', created 'concepts/negative-cycles.md', "
            "and saved your progress in memory."
        )
    )

    engine = AgentEngine(
        llm_client=mock_llm,
        tool_registry=registry,
        config=AgentConfig(max_iterations=12),
    )
    context = ContextManager()

    user_prompt = (
        "I need to review Dijkstra vs Bellman-Ford for my exam. Check what I struggled with earlier, "
        "inspect our vault notes, research negative cycle detection, update the notes, and record my progress."
    )
    result = engine.run(user_prompt, context)

    # --- Verification & Assertions ---
    assert result.is_success is True
    assert result.tool_calls_count == 8

    # 1. Verify Memory Updates
    facts = store.recall_facts("Mastered negative cycle")
    assert len(facts) >= 1
    assert "Mastered negative cycle" in facts[0].fact

    # 2. Verify MCP Vault Updates & Section Insertion
    dijkstra_text = (concepts_dir / "dijkstra.md").read_text(encoding="utf-8")
    assert "Dijkstra fails with negative edge weights" in dijkstra_text
    assert "[[bellman-ford]]" in dijkstra_text

    # 3. Verify New Note Creation via MCP
    assert (concepts_dir / "negative-cycles.md").is_file()
    neg_cycles_text = (concepts_dir / "negative-cycles.md").read_text(encoding="utf-8")
    assert "Negative Weight Cycles" in neg_cycles_text

    # 4. Verify Obsidian Knowledge Graph Linting
    linter = VaultLinter(artifacts_dir)
    report = linter.lint()
    assert report.total_files >= 2
    assert (concepts_dir / "dijkstra.md").exists()

    store.close()
