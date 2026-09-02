from pathlib import Path
from study_agent.config.settings import AgentConfig, FilesystemToolConfig
from study_agent.core.context import ContextManager
from study_agent.core.engine import AgentEngine
from study_agent.core.types import ToolCall
from study_agent.llm.mock import MockLLMClient
from study_agent.study.griller import SocraticGriller
from study_agent.study.linter import VaultLinter
from study_agent.study.templates import format_concept_note
from study_agent.tools.base import ToolRegistry
from study_agent.tools.filesystem import register_filesystem_tools


def test_e2e_lecture_ingestion_and_concept_linking(tmp_path: Path):
    """
    E2E Test: Ingestion and Concept Linking
    1. Student drops lecture note into workspace/data/
    2. Agent reads data/lecture_graphs.txt
    3. Agent creates artifacts/lectures/lecture-04.md linking [[dijkstra]]
    4. Agent creates artifacts/concepts/dijkstra.md linking [[bellman-ford]]
    5. VaultLinter verifies link structures
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data_dir = workspace / "data"
    data_dir.mkdir()
    artifacts_dir = workspace / "artifacts"
    artifacts_dir.mkdir()
    (artifacts_dir / "lectures").mkdir()
    (artifacts_dir / "concepts").mkdir()

    # 1. Source material in data/
    (data_dir / "lecture_graphs.txt").write_text(
        "Lecture 04: Graph Algorithms\nTopics: Dijkstra and Bellman-Ford.\n"
        "Dijkstra computes single-source shortest paths on non-negative weighted graphs.",
        encoding="utf-8"
    )

    config = FilesystemToolConfig(
        allowed_read_paths=[str(workspace)],
        allowed_write_paths=[str(artifacts_dir)]
    )
    registry = ToolRegistry()
    register_filesystem_tools(registry, base_dir=workspace, config=config)

    mock_llm = MockLLMClient()
    # Turn 1: Read source material
    mock_llm.add_response(
        content="Reading raw lecture file...",
        tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "data/lecture_graphs.txt"})]
    )
    # Turn 2: Write lecture note with [[dijkstra]]
    lecture_content = (
        "---\ntitle: Lecture 04 - Graph Algorithms\nstatus: current\n---\n"
        "# Lecture 04\n\n## Linked Concepts\n- [[dijkstra]]\n- [[bellman-ford]]\n"
    )
    mock_llm.add_response(
        content="Writing lecture summary...",
        tool_calls=[ToolCall(id="c2", name="write_file", arguments={"path": "artifacts/lectures/lecture-04.md", "content": lecture_content})]
    )
    # Turn 3: Write concept note dijkstra.md
    concept_content = format_concept_note(
        title="Dijkstra Algorithm",
        course="Graph Algorithms",
        one_line_summary="Single source shortest paths with non-negative weights.",
        core_intuition="Greedy edge relaxation.",
        formal_definition="Computes dist(s, v) for all v in V.",
        connections=["bellman-ford"]
    )
    mock_llm.add_response(
        content="Writing concept note...",
        tool_calls=[ToolCall(id="c3", name="write_file", arguments={"path": "artifacts/concepts/dijkstra.md", "content": concept_content})]
    )
    # Turn 4: Final answer
    mock_llm.add_response(
        content="Ingested Lecture 04 and synthesized the linked concept note for Dijkstra's Algorithm."
    )

    engine = AgentEngine(llm_client=mock_llm, tool_registry=registry, config=AgentConfig(max_iterations=8))
    context = ContextManager()

    prompt = "Ingest data/lecture_graphs.txt and compile lecture-04.md and dijkstra.md."
    result = engine.run(prompt, context)

    assert result.is_success is True
    assert (artifacts_dir / "lectures" / "lecture-04.md").is_file()
    assert (artifacts_dir / "concepts" / "dijkstra.md").is_file()

    # Also pre-create bellman-ford so links are complete
    (artifacts_dir / "concepts" / "bellman-ford.md").write_text(
        format_concept_note(
            title="Bellman Ford",
            course="Graph Algorithms",
            one_line_summary="Shortest paths with negative weights.",
            core_intuition="Dynamic programming relaxation.",
            formal_definition="Relax all edges |V|-1 times.",
            connections=["dijkstra"]
        ),
        encoding="utf-8"
    )

    # Lint the resulting vault
    linter = VaultLinter(artifacts_dir)
    report = linter.lint()
    assert report.total_files == 3
    assert len(report.broken_links) == 0
    assert report.is_healthy is True


def test_e2e_socratic_grilling_prompt_flow():
    """Verify Socratic grilling prompt formulation and answer evaluation."""
    # 1. Initial Prompt
    start_prompt = SocraticGriller.create_initial_prompt(
        topic="Dynamic Programming",
        context_material="Optimal substructure and overlapping subproblems."
    )
    assert "Dynamic Programming" in start_prompt
    assert "Optimal substructure" in start_prompt
    assert "Level 1" in start_prompt

    # 2. Evaluation Prompt
    eval_prompt = SocraticGriller.create_evaluation_prompt(
        topic="Dynamic Programming",
        question="What is the difference between optimal substructure and overlapping subproblems?",
        student_answer="Optimal substructure means optimal solution contains optimal solutions to subproblems."
    )
    assert "Dynamic Programming" in eval_prompt
    assert "Precision Critique" in eval_prompt
