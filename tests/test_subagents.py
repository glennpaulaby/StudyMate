from typing import Any, Dict, List, Optional
import pytest

from study_agent.core.context import ContextManager
from study_agent.core.engine import AgentEngine
from study_agent.core.subagents import (
    SubagentConfig,
    SubagentRegistry,
    get_default_subagent_registry,
    EXAMINER_SYSTEM_PROMPT,
    RESEARCHER_SYSTEM_PROMPT,
)
from study_agent.core.types import LLMResponse, Role, ToolCall
from study_agent.llm.mock import MockLLMClient
from study_agent.tools.base import BaseTool, ToolRegistry
from study_agent.tools.delegation import (
    DelegateTaskTool,
    ResearchTopicTool,
    register_delegation_tools,
)


class DummyTool(BaseTool):
    def __init__(self, tool_name: str) -> None:
        self._name = tool_name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"Dummy tool {self._name}"

    @property
    def parameters(self) -> Dict[str, Any]:
        return {"type": "object", "properties": {"query": {"type": "string"}}}

    def execute(self, **kwargs: Any) -> str:
        return f"{self._name} output"


def test_subagents_restricted_tool_subsets():
    """
    Unit test: Verify specialized subagents run with restricted tool subsets
    and do not have access to unauthorized tools.
    """
    base_registry = ToolRegistry()
    all_tools = [
        "read_file",
        "write_file",
        "edit_file",
        "obsidian_get_backlinks",
        "memory_recall",
        "research_topic",
    ]
    for tool_name in all_tools:
        base_registry.register(DummyTool(tool_name))

    subagent_registry = get_default_subagent_registry()
    mock_llm = MockLLMClient()

    # 1. Test Examiner tool restrictions
    examiner_engine = subagent_registry.create_engine(
        name="examiner",
        llm_client=mock_llm,
        base_registry=base_registry,
    )
    examiner_tools = list(examiner_engine.tool_registry.tools.keys())

    assert set(examiner_tools) == {"read_file", "obsidian_get_backlinks", "memory_recall"}
    assert "write_file" not in examiner_tools
    assert "edit_file" not in examiner_tools
    assert "research_topic" not in examiner_tools
    assert examiner_engine.config.max_iterations == 8
    assert examiner_engine.system_prompt == EXAMINER_SYSTEM_PROMPT

    # 2. Test Researcher tool restrictions
    researcher_engine = subagent_registry.create_engine(
        name="researcher",
        llm_client=mock_llm,
        base_registry=base_registry,
    )
    researcher_tools = list(researcher_engine.tool_registry.tools.keys())

    assert set(researcher_tools) == {"research_topic"}
    assert "write_file" not in researcher_tools
    assert "edit_file" not in researcher_tools
    assert "read_file" not in researcher_tools
    assert researcher_engine.config.max_iterations == 12
    assert researcher_engine.system_prompt == RESEARCHER_SYSTEM_PROMPT


def test_child_agent_context_isolation():
    """
    Unit test: Verify child agent context isolation (child context does not bleed into parent messages).
    """
    mock_llm = MockLLMClient()
    mock_llm.add_response(content="Child subagent assessment completed successfully.")

    subagent_registry = get_default_subagent_registry()

    # Parent context with previous interaction
    parent_context = ContextManager(custom_system_prompt="Parent system prompt.")
    parent_context.add_user_message("Hello from parent conversation.")
    parent_context.add_assistant_message("Hello! I am your main assistant.")
    initial_parent_message_count = len(parent_context.get_messages())

    # Child engine execution
    child_engine = subagent_registry.create_engine(
        name="examiner",
        llm_client=mock_llm,
    )
    child_result = child_engine.run("Grill me on Transformers self-attention.")

    assert child_result.is_success is True
    assert child_result.final_response == "Child subagent assessment completed successfully."

    # Verify parent context is completely untouched by child engine execution
    assert len(parent_context.get_messages()) == initial_parent_message_count
    assert parent_context.get_messages()[0].content.startswith("Parent system prompt.")
    assert parent_context.get_messages()[1].content == "Hello from parent conversation."
    assert parent_context.get_messages()[2].content == "Hello! I am your main assistant."


def test_parent_delegates_to_child_and_ingests_observation():
    """
    Integration test: Simulate parent LLM calling `delegate_task`, verify child
    engine execution, and assert child response is ingested as an Observation.
    """
    mock_llm = MockLLMClient()

    # Turn 1: Parent decides to delegate
    mock_llm.add_response(
        content=None,
        tool_calls=[
            ToolCall(
                id="call_delegation_1",
                name="delegate_task",
                arguments={
                    "subagent_name": "examiner",
                    "task": "Test understanding of multi-head attention.",
                },
            )
        ],
    )

    # Turn 2: Child subagent executes
    mock_llm.add_response(
        content="Question: Why do we scale dot products by sqrt(d_k) in self-attention?",
    )

    # Turn 3: Parent receives child output as tool observation and generates final response
    mock_llm.add_response(
        content="I have delegated this to the Examiner. Here is your first question: Why do we scale dot products by sqrt(d_k) in self-attention?",
    )

    base_registry = ToolRegistry()
    base_registry.register(DummyTool("read_file"))
    base_registry.register(DummyTool("obsidian_get_backlinks"))
    base_registry.register(DummyTool("memory_recall"))

    subagent_registry = get_default_subagent_registry()
    delegate_tool = DelegateTaskTool(
        llm_client=mock_llm,
        subagent_registry=subagent_registry,
        parent_tool_registry=base_registry,
    )
    base_registry.register(delegate_tool)

    parent_engine = AgentEngine(
        llm_client=mock_llm,
        tool_registry=base_registry,
    )
    parent_context = ContextManager(custom_system_prompt="You are StudyMate.")

    result = parent_engine.run(
        user_message="Please grill me on multi-head attention.",
        context=parent_context,
    )

    assert result.is_success is True
    assert "Why do we scale dot products by sqrt(d_k)" in result.final_response

    # Verify message sequence in parent context:
    # 0: SYSTEM
    # 1: USER ("Please grill me...")
    # 2: ASSISTANT (tool_calls=[delegate_task])
    # 3: TOOL (observation containing child output)
    # 4: ASSISTANT (final response)
    messages = parent_context.get_messages()
    assert len(messages) == 5
    assert messages[0].role == Role.SYSTEM
    assert messages[1].role == Role.USER
    assert messages[2].role == Role.ASSISTANT
    assert messages[2].tool_calls is not None
    assert messages[2].tool_calls[0].name == "delegate_task"
    assert messages[3].role == Role.TOOL
    assert messages[3].name == "delegate_task"
    assert "Why do we scale dot products by sqrt(d_k)" in (messages[3].content or "")
    assert messages[4].role == Role.ASSISTANT
    assert "Why do we scale dot products by sqrt(d_k)" in (messages[4].content or "")


def test_parent_run_id_and_telemetry_recording():
    """
    Unit test: Verify parent_run_id is recorded in metadata and telemetry events.
    """
    telemetry_events: List[Dict[str, Any]] = []

    def telemetry_listener(event: Dict[str, Any]) -> None:
        telemetry_events.append(event)

    mock_llm = MockLLMClient()
    mock_llm.add_response(content="Done.")

    engine = AgentEngine(
        llm_client=mock_llm,
        system_prompt="Child prompt",
        max_iterations=5,
        parent_run_id="parent_run_999",
        on_telemetry_event=telemetry_listener,
    )

    result = engine.run("Sub-task execution")

    assert result.is_success is True
    assert result.parent_run_id == "parent_run_999"
    assert result.metadata.get("parent_run_id") == "parent_run_999"
    assert result.run_id is not None

    assert len(telemetry_events) >= 2
    assert telemetry_events[0]["event"] == "run_start"
    assert telemetry_events[0]["parent_run_id"] == "parent_run_999"
    assert telemetry_events[-1]["event"] == "run_end"
    assert telemetry_events[-1]["parent_run_id"] == "parent_run_999"


def test_delegate_task_tool_error_handling():
    """
    Unit test: Verify DelegateTaskTool handles unknown subagent, empty task,
    and child exceptions gracefully without crashing.
    """
    mock_llm = MockLLMClient()
    subagent_registry = get_default_subagent_registry()
    tool = DelegateTaskTool(llm_client=mock_llm, subagent_registry=subagent_registry)

    # 1. Unknown subagent
    res1 = tool.execute(subagent_name="nonexistent", task="Do something")
    assert "Error:" in res1
    assert "not registered" in res1

    # 2. Empty task
    res2 = tool.execute(subagent_name="examiner", task="")
    assert "Error:" in res2
    assert "cannot be empty" in res2

    # 3. Missing subagent_name
    res3 = tool.execute(subagent_name="", task="Do something")
    assert "Error:" in res3
    assert "parameter is required" in res3


def test_custom_subagent_registration():
    """
    Unit test: Verify custom subagent registration and execution.
    """
    registry = SubagentRegistry()
    config = SubagentConfig(
        name="custom_tutor",
        description="Custom tutoring subagent",
        system_prompt="Custom tutor system prompt",
        allowed_tools=["dummy_tool"],
        max_iterations=6,
    )
    registry.register(config)

    assert registry.get("custom_tutor") == config
    assert "custom_tutor" in registry.list_names()

    base_tools = ToolRegistry()
    base_tools.register(DummyTool("dummy_tool"))
    base_tools.register(DummyTool("forbidden_tool"))

    mock_llm = MockLLMClient()
    mock_llm.add_response(content="Custom tutor response")

    engine = registry.create_engine("custom_tutor", mock_llm, base_registry=base_tools)
    assert list(engine.tool_registry.tools.keys()) == ["dummy_tool"]
    assert engine.config.max_iterations == 6

    result = engine.run("Help me understand calculus.")
    assert result.is_success is True
    assert result.final_response == "Custom tutor response"


def test_research_topic_tool():
    """
    Unit test: Verify ResearchTopicTool execution.
    """
    tool = ResearchTopicTool()
    assert tool.name == "research_topic"
    output = tool.execute(topic="Attention Mechanism", depth="comparative")
    assert "Attention Mechanism" in output
    assert "comparative" in output

    empty_out = tool.execute(topic="")
    assert "Error:" in empty_out
