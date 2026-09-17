"""E2E: delegation + permission enforcement + tools + observability in one run.

PDF Week 3 mandate: at least one end-to-end test covering sub-agent
delegation, permission enforcement, tool execution, and observability
together. Mirrors the terminal REPL wiring (policy registry, delegation
fan-out, permission batching) against a scripted mock LLM.
"""

import json
import re

from study_agent.config.settings import ObservabilityConfig
from study_agent.core.context import ContextManager
from study_agent.core.engine import AgentEngine
from study_agent.core.subagents import get_default_subagent_registry
from study_agent.core.types import ToolCall
from study_agent.llm.mock import MockLLMClient
from study_agent.observability import metrics
from study_agent.observability.events import build_permission_event
from study_agent.observability.tracing import append_trace
from study_agent.security.policy import Decision, PermissionPolicy, Rule
from study_agent.tools.base import BaseTool, ToolRegistry
from study_agent.tools.delegation import DelegateTaskTool


class _ResearchStub(BaseTool):
    """Stands in for the researcher's exploration tool."""

    @property
    def name(self):
        return "research_topic"

    @property
    def description(self):
        return "stub research"

    @property
    def parameters(self):
        return {"type": "object", "properties": {"topic": {"type": "string"}}}

    def execute(self, **kwargs):
        return "stub findings"


def _series_value(text: str, name: str, labels: str) -> float:
    pattern = re.compile(rf"^{re.escape(name)}\{{{re.escape(labels)}\}} ([0-9.e+-]+)$", re.M)
    match = pattern.search(text)
    assert match, f"series {name}{{{labels}}} missing from exposition"
    return float(match.group(1))


def test_e2e_delegation_permission_tools_observability(tmp_path):
    """Full loop: gated delegate_task runs a child, both fan out to traces."""
    trace_file = tmp_path / "traces.jsonl"
    obs = ObservabilityConfig(metrics_enabled=False, trace_file=str(trace_file))

    mock_llm = MockLLMClient()
    mock_llm.add_response(
        content=None,
        tool_calls=[
            ToolCall(
                id="call_1",
                name="delegate_task",
                arguments={"subagent_name": "researcher", "task": "summarize X"},
            )
        ],
    )
    mock_llm.add_response(
        content=None,
        tool_calls=[
            ToolCall(
                id="call_child_1",
                name="research_topic",
                arguments={"topic": "X"},
            )
        ],
    )
    mock_llm.add_response(content="child synthesis")
    mock_llm.add_response(content="parent final")
    policy = PermissionPolicy(
        rules=[
            Rule(tools=["delegate_task"], decision=Decision.ALLOW, tier="sandbox-edit"),
            Rule(tools=["research_topic"], decision=Decision.ALLOW, tier="observe"),
        ],
        default_decision=Decision.DENY,
    )
    registry = ToolRegistry(policy=policy)
    registry.register(_ResearchStub())

    permission_seen = []
    registry.on_permission_decision.append(permission_seen.append)

    subagent_events = []
    delegate_tool = DelegateTaskTool(
        llm_client=mock_llm,
        subagent_registry=get_default_subagent_registry(),
        parent_tool_registry=registry,
        observability=obs,
        on_subagent_run=lambda event: (
            subagent_events.append(event),
            append_trace(str(trace_file), event),
        ),
    )
    registry.register(delegate_tool)

    engine = AgentEngine(
        llm_client=mock_llm, tool_registry=registry, observability=obs
    )
    context = ContextManager(custom_system_prompt="sys")
    result = engine.run(user_message="research X", context=context)

    # Delegation + tool execution worked end to end: the child output was
    # ingested as the delegate_task observation, the parent closed the run.
    assert result.is_success is True
    assert result.final_response == "parent final"
    tool_msgs = [m for m in context.get_messages() if m.role.value == "tool"]
    assert any("child synthesis" in (m.content or "") for m in tool_msgs)

    # Permission enforcement saw both the delegation and the child tool call.
    gated = {e.tool for e in permission_seen}
    assert "delegate_task" in gated
    assert "research_topic" in gated

    # Subagent fan-out recorded exactly one delegation, linked to the parent.
    assert len(subagent_events) == 1
    assert subagent_events[0]["tools"][0]["name"] == "subagent:researcher"
    assert subagent_events[0]["parent_run_id"] == result.run_id

    # Permission batch fans out the same way the REPL flush does.
    batch_event = build_permission_event(permission_seen, model="mock")
    append_trace(str(trace_file), batch_event)

    lines = [
        json.loads(line)
        for line in trace_file.read_text(encoding="utf-8").splitlines()
    ]
    names = [t["name"] for line in lines for t in line.get("tools", [])]
    assert "subagent:researcher" in names
    assert "permission:delegate_task" in names
    assert "permission:research_topic" in names


def test_e2e_delegation_permission_prometheus_metrics(tmp_path):
    """Full loop with active Prometheus exporter: parent delegation -> permission -> child execution -> Prometheus metrics."""
    assert metrics.init_metrics(True, host="127.0.0.1", port=18129) is True

    trace_file = tmp_path / "traces_metrics.jsonl"
    obs = ObservabilityConfig(metrics_enabled=True, trace_file=str(trace_file))

    mock_llm = MockLLMClient()
    mock_llm.add_response(
        content=None,
        tool_calls=[
            ToolCall(
                id="call_deleg_p1",
                name="delegate_task",
                arguments={"subagent_name": "researcher", "task": "analyze transformers"},
            )
        ],
    )
    mock_llm.add_response(
        content=None,
        tool_calls=[
            ToolCall(
                id="call_child_res",
                name="research_topic",
                arguments={"topic": "Transformers"},
            )
        ],
    )
    mock_llm.add_response(content="Transformers are attention-based neural architectures.")
    mock_llm.add_response(content="Parent synthesis with research included.")

    policy = PermissionPolicy(
        rules=[
            Rule(tools=["delegate_task"], decision=Decision.ALLOW),
            Rule(tools=["research_topic"], decision=Decision.ALLOW),
        ],
        default_decision=Decision.DENY,
    )
    registry = ToolRegistry(policy=policy)
    registry.register(_ResearchStub())

    # Wire permission metrics emission
    def _observe_perm(event):
        metrics.observe_permission(event.tool, event.decision.value)

    registry.on_permission_decision.append(_observe_perm)

    delegate_tool = DelegateTaskTool(
        llm_client=mock_llm,
        subagent_registry=get_default_subagent_registry(),
        parent_tool_registry=registry,
        observability=obs,
    )
    registry.register(delegate_tool)

    engine = AgentEngine(
        llm_client=mock_llm, tool_registry=registry, observability=obs
    )
    result = engine.run("Research Transformers", ContextManager(custom_system_prompt="sys"))

    assert result.is_success is True
    assert "Parent synthesis" in result.final_response

    # Verify Prometheus metrics exposition
    text = metrics.latest_metrics_text()
    assert _series_value(text, "studymate_permission_decisions_total", 'decision="allow",tool="delegate_task"') >= 1.0
    assert _series_value(text, "studymate_permission_decisions_total", 'decision="allow",tool="research_topic"') >= 1.0
    assert _series_value(text, "studymate_subagent_runs_total", 'config="researcher",status="success"') >= 1.0
    assert _series_value(text, "studymate_tool_calls_total", 'source="builtin",status="ok",tool="delegate_task"') >= 1.0
    assert _series_value(text, "studymate_tool_calls_total", 'source="builtin",status="ok",tool="research_topic"') >= 1.0
    assert "studymate_agent_runs_total" in text
