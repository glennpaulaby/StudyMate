from pathlib import Path
from study_agent.config.settings import AgentConfig, FilesystemToolConfig
from study_agent.core.context import ContextManager
from study_agent.core.engine import AgentEngine
from study_agent.core.types import LLMResponse, ToolCall
from study_agent.llm.mock import MockLLMClient
from study_agent.tools.base import ToolRegistry
from study_agent.tools.filesystem import register_filesystem_tools


def test_react_single_turn_no_tools():
    """Verify ReAct loop handles direct responses without tool calling."""
    mock_llm = MockLLMClient()
    mock_llm.add_response(content="Dynamic Programming is an optimization technique.")

    registry = ToolRegistry()
    engine = AgentEngine(llm_client=mock_llm, tool_registry=registry, config=AgentConfig(max_iterations=5))
    context = ContextManager()

    result = engine.run("What is Dynamic Programming?", context)

    assert result.is_success is True
    assert result.iterations == 1
    assert result.tool_calls_count == 0
    assert "Dynamic Programming is an optimization technique." in result.final_response
    assert context.user_turn_count == 1


def test_react_multi_step_tool_execution(tmp_path: Path):
    """Verify ReAct loop executes tools and provides observation back to LLM."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifacts = workspace / "artifacts"
    artifacts.mkdir()

    # Pre-populate a file
    (artifacts / "summary.txt").write_text("Eulerian path visits every edge exactly once.", encoding="utf-8")

    registry = ToolRegistry()
    config = FilesystemToolConfig(allowed_read_paths=[str(workspace)], allowed_write_paths=[str(artifacts)])
    register_filesystem_tools(registry, base_dir=workspace, config=config)

    mock_llm = MockLLMClient()
    # Step 1: LLM decides to read the file
    mock_llm.add_response(
        content=None,
        tool_calls=[ToolCall(id="call_read", name="read_file", arguments={"path": "artifacts/summary.txt"})]
    )
    # Step 2: After seeing observation, LLM answers
    mock_llm.add_response(content="An Eulerian path visits every edge in the graph exactly once.")

    tool_started = []
    tool_ended = []

    def on_start(tc: ToolCall):
        tool_started.append(tc.name)

    def on_end(tc: ToolCall, obs: str):
        tool_ended.append((tc.name, obs))

    engine = AgentEngine(llm_client=mock_llm, tool_registry=registry, config=AgentConfig(max_iterations=5))
    context = ContextManager()

    result = engine.run(
        "What does summary.txt say?",
        context,
        on_tool_start=on_start,
        on_tool_end=on_end
    )

    assert result.is_success is True
    assert result.iterations == 2
    assert result.tool_calls_count == 1
    assert "Eulerian path" in result.final_response
    assert tool_started == ["read_file"]
    assert len(tool_ended) == 1
    assert "Eulerian path visits every edge" in tool_ended[0][1]


def test_react_continues_after_length_truncated_response():
    """Verify a length-truncated model response gets a continuation turn."""
    mock_llm = MockLLMClient(
        responses=[
            LLMResponse(content="The summary is incomplete", finish_reason="length"),
            LLMResponse(content="The lecture was ingested successfully."),
        ]
    )

    engine = AgentEngine(
        llm_client=mock_llm,
        tool_registry=ToolRegistry(),
        config=AgentConfig(max_iterations=3),
    )
    result = engine.run("Ingest the lecture material.", ContextManager())

    assert result.is_success is True
    assert result.iterations == 2
    assert result.final_response == "The lecture was ingested successfully."
    assert "Continue from where you stopped." in mock_llm.call_history[1][-1].content


def test_react_continues_after_empty_response():
    """Verify an empty terminal response does not stop a tool workflow."""
    mock_llm = MockLLMClient(
        responses=[
            LLMResponse(content=None),
            LLMResponse(content="Ingestion completed."),
        ]
    )

    engine = AgentEngine(
        llm_client=mock_llm,
        tool_registry=ToolRegistry(),
        config=AgentConfig(max_iterations=3),
    )
    result = engine.run("Ingest the lecture material.", ContextManager())

    assert result.is_success is True
    assert result.iterations == 2
    assert result.final_response == "Ingestion completed."
    assert "Your last response was empty." in mock_llm.call_history[1][-1].content


def test_react_reports_repeated_empty_response_as_failure():
    """Verify repeated empty model output is surfaced instead of reported as success."""
    mock_llm = MockLLMClient(
        responses=[
            LLMResponse(content=None),
            LLMResponse(content=None),
        ]
    )
    result = AgentEngine(
        llm_client=mock_llm,
        tool_registry=ToolRegistry(),
        config=AgentConfig(max_iterations=2),
    ).run("Replace x with y.", ContextManager())

    assert result.is_success is False
    assert result.error_message is not None
    assert "empty response" in result.final_response


def test_react_max_iterations_bound():
    """Verify ReAct loop terminates gracefully when max_iterations is reached."""
    mock_llm = MockLLMClient()
    # Script endless tool calls
    for i in range(10):
        mock_llm.add_response(
            content=None,
            tool_calls=[ToolCall(id=f"call_{i}", name="list_files", arguments={"path": "."})]
        )

    registry = ToolRegistry()
    engine = AgentEngine(llm_client=mock_llm, tool_registry=registry, config=AgentConfig(max_iterations=3))
    context = ContextManager()

    result = engine.run("List files repeatedly", context)

    assert result.is_success is False
    assert result.iterations == 3
    assert result.tool_calls_count == 3
    assert "maximum number of reasoning steps" in result.final_response
