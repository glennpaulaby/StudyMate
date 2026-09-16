"""Unit tests for subagent monitoring (Week 3 Phase 1).

Covers the ``subagent:<name>`` trace events, the ``observe_subagent``
Prometheus hook, parent-run linkage stamped by the engine, and
best-effort degradation (telemetry failures never break a delegation).
"""

from study_agent.core.context import ContextManager
from study_agent.core.engine import AgentEngine
from study_agent.core.subagents import get_default_subagent_registry
from study_agent.core.types import ToolCall
from study_agent.llm.mock import MockLLMClient
from study_agent.observability.events import (
    SUBAGENT_THREAD_ID,
    SUBAGENT_THREAD_NAME,
    build_subagent_event,
)
from study_agent.tools import delegation as delegation_module
from study_agent.tools.base import ToolRegistry
from study_agent.tools.delegation import DelegateTaskTool


def _delegate_tool(mock_llm, **kwargs):
    base_registry = ToolRegistry()
    tool = DelegateTaskTool(
        llm_client=mock_llm,
        subagent_registry=get_default_subagent_registry(),
        parent_tool_registry=base_registry,
        **kwargs,
    )
    base_registry.register(tool)
    return base_registry, tool


def test_build_subagent_event_schema():
    """Verify the subagent event shape widgets filter on."""
    event = build_subagent_event(
        subagent_name="researcher",
        status="success",
        parent_run_id="run_parent",
        child_run_id="run_child",
        iterations=3,
        prompt_tokens=10,
        completion_tokens=20,
        reasoning_tokens=5,
        duration_s=1.25,
        summary="done",
    )
    assert event["schema"] == 2
    assert event["run_id"] == "run_child"
    assert event["parent_run_id"] == "run_parent"
    assert event["thread"] == {"id": SUBAGENT_THREAD_ID, "name": SUBAGENT_THREAD_NAME}
    assert event["status"] == "success"
    assert event["tokens"]["reasoning"] == 5
    record = event["tools"][0]
    assert record["name"] == "subagent:researcher"
    assert record["source"] == "subagent"
    assert record["status"] == "ok"
    assert record["arguments"] == {"parent_run_id": "run_parent"}


def test_build_subagent_event_error_mapping():
    """Verify timeouts/errors surface as error status with detail preserved."""
    event = build_subagent_event(
        subagent_name="examiner",
        status="error",
        error="timed out: slow",
    )
    assert event["status"] == "error"
    assert event["error"] == "timed out: slow"
    assert event["tools"][0]["status"] == "error"
    assert event["run_id"]  # generated when no child id is known


def test_delegate_emits_metric_and_event(monkeypatch):
    """Verify a delegation fans out to Prometheus hook + trace callback."""
    mock_llm = MockLLMClient()
    mock_llm.add_response(content="child says hi")
    events = []
    metrics_calls = []
    monkeypatch.setattr(
        delegation_module,
        "observe_subagent",
        lambda config, status: metrics_calls.append((config, status)),
    )
    _, tool = _delegate_tool(mock_llm, on_subagent_run=events.append)

    observation = tool.execute(subagent_name="researcher", task="summarize this")

    assert observation == "child says hi"
    assert metrics_calls == [("researcher", "success")]
    assert len(events) == 1
    assert events[0]["tools"][0]["name"] == "subagent:researcher"
    assert events[0]["status"] == "success"


def test_delegate_failure_records_error(monkeypatch):
    """Verify a failed child run records an error event, observation surfaces it."""
    from study_agent.core.types import AgentRunResult

    mock_llm = MockLLMClient()
    events = []
    metrics_calls = []
    monkeypatch.setattr(
        delegation_module,
        "observe_subagent",
        lambda config, status: metrics_calls.append((config, status)),
    )
    _, tool = _delegate_tool(mock_llm, on_subagent_run=events.append)

    class _FailingEngine:
        def run(self, task):
            return AgentRunResult(
                final_response="",
                iterations=2,
                is_success=False,
                error_message="boom",
                run_id="run_child_fail",
            )

    monkeypatch.setattr(tool.subagent_registry, "create_engine", lambda **kw: _FailingEngine())

    observation = tool.execute(subagent_name="researcher", task="x")

    assert observation == "Subagent 'researcher' failed: boom"
    assert metrics_calls == [("researcher", "error")]
    assert len(events) == 1
    assert events[0]["status"] == "error"
    assert events[0]["run_id"] == "run_child_fail"


def test_unknown_subagent_returns_before_any_run():
    """Verify validation errors return before any run exists (no event)."""
    mock_llm = MockLLMClient()
    _, tool = _delegate_tool(mock_llm)
    events = []
    tool.on_subagent_run = events.append

    observation = tool.execute(subagent_name="nope", task="x")

    assert observation.startswith("Error: Subagent 'nope' is not registered.")
    assert events == []  # validation errors return before any run exists


def test_engine_stamps_parent_run_id():
    """Verify the parent run id lands on the delegation tool and event."""
    mock_llm = MockLLMClient()
    mock_llm.add_response(
        content=None,
        tool_calls=[
            ToolCall(
                id="call_1",
                name="delegate_task",
                arguments={"subagent_name": "researcher", "task": "go"},
            )
        ],
    )
    mock_llm.add_response(content="child answer")
    mock_llm.add_response(content="parent done")
    events = []
    registry, tool = _delegate_tool(mock_llm, on_subagent_run=events.append)

    engine = AgentEngine(llm_client=mock_llm, tool_registry=registry)
    result = engine.run(
        user_message="delegate please",
        context=ContextManager(custom_system_prompt="sys"),
    )

    assert result.is_success is True
    assert tool.parent_run_id == result.run_id
    assert len(events) == 1
    assert events[0]["parent_run_id"] == result.run_id
    assert events[0]["run_id"]  # child run id, distinct from the parent


def test_degraded_callback_never_breaks_delegation():
    """Verify a raising trace callback still returns the child output."""
    mock_llm = MockLLMClient()
    mock_llm.add_response(content="child answer")

    def _boom(event):
        raise RuntimeError("sink down")

    _, tool = _delegate_tool(mock_llm, on_subagent_run=_boom)

    assert tool.execute(subagent_name="examiner", task="go") == "child answer"


def test_timeout_records_error_event():
    """Verify a child timeout records an error event with detail."""
    mock_llm = MockLLMClient()
    events = []
    registry = get_default_subagent_registry()
    tool = DelegateTaskTool(
        llm_client=mock_llm,
        subagent_registry=registry,
        on_subagent_run=events.append,
    )

    def _raise(*args, **kwargs):
        raise TimeoutError("too slow")

    registry.create_engine = _raise  # type: ignore[method-assign]

    observation = tool.execute(subagent_name="examiner", task="go")

    assert "timed out" in observation
    assert len(events) == 1
    assert events[0]["status"] == "error"
    assert "too slow" in events[0]["error"]
