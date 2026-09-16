"""E2E benchmark: one autonomous run emits metrics exposition + linked trace.

Mirrors the `test_mcp_memory_scenario` pattern (tmp workspace, scripted
`MockLLMClient`, real `MemoryStore`) and asserts the Week 3 Task 1
deliverables: the `/metrics` exposition names the run series and the
trace file holds exactly one JSON line for the same run.
"""

import json
from pathlib import Path
from typing import Any, Dict

from study_agent.config.settings import FilesystemToolConfig, ObservabilityConfig
from study_agent.core.context import ContextManager
from study_agent.core.engine import AgentEngine
from study_agent.core.types import ToolCall
from study_agent.llm.mock import MockLLMClient
from study_agent.memory.store import MemoryStore
from study_agent.memory.tools import register_memory_tools
from study_agent.observability import metrics
from study_agent.tools.base import BaseTool, ToolRegistry
from study_agent.tools.filesystem import register_filesystem_tools

E2E_METRICS_PORT = 18124


class _VaultProbeTool(BaseTool):
    """Minimal stand-in for an MCP vault tool (module retargeted below)."""

    @property
    def name(self) -> str:
        return "vault_probe"

    @property
    def description(self) -> str:
        return "Probe stand-in for MCP-sourced tools."

    @property
    def parameters(self) -> Dict[str, Any]:
        return {"type": "object", "properties": {}}

    def execute(self, **kwargs: Any) -> str:
        return "vault intact"


# Retarget the probe so source inference files it under the MCP tier,
# matching how real `study_agent.tools.mcp` tools classify.
_VaultProbeTool.__module__ = "study_agent.tools.mcp.tools"


def test_observability_e2e_run(tmp_path: Path):
    """Verify a full run links exposition series to exactly one trace line."""
    workspace = tmp_path / "workspace"
    artifacts = workspace / "artifacts"
    artifacts.mkdir(parents=True)

    metrics.init_metrics(True, host="127.0.0.1", port=E2E_METRICS_PORT)

    registry = ToolRegistry()
    register_filesystem_tools(
        registry,
        base_dir=workspace,
        config=FilesystemToolConfig(
            allowed_read_paths=[str(workspace)],
            allowed_write_paths=[str(artifacts)],
        ),
    )
    registry.register(_VaultProbeTool())
    store = MemoryStore(str(artifacts / "memory" / "memory.db"))
    register_memory_tools(registry, store)

    llm = MockLLMClient()
    llm.add_response(
        content="",
        tool_calls=[ToolCall(id="e1", name="vault_probe", arguments={})],
    )
    llm.add_response(
        content="",
        tool_calls=[
            ToolCall(
                id="e2",
                name="memory_store",
                arguments={"topic": "e2e", "fact": "observability links runs to traces"},
            )
        ],
    )
    llm.add_response(content="Run complete: probe ok, fact stored.")

    trace_file = tmp_path / "traces.jsonl"
    obs = ObservabilityConfig(metrics_enabled=True, trace_file=str(trace_file))
    engine = AgentEngine(llm_client=llm, tool_registry=registry, observability=obs)
    result = engine.run("probe the vault and remember this run", ContextManager())

    assert result.is_success is True
    assert result.tool_calls_count == 2

    # 1. Metrics exposition names the run series.
    text = metrics.latest_metrics_text()
    assert "studymate_agent_runs_total" in text
    assert "studymate_tool_calls_total" in text
    assert "studymate_memory_ops_total" in text

    # 2. Exactly one trace line, linked to the same run's tool records.
    lines = trace_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["status"] == "success"
    assert event["iterations"] == result.iterations
    assert [t["name"] for t in event["tools"]] == ["vault_probe", "memory_store"]
    assert [t["source"] for t in event["tools"]] == ["mcp", "memory"]
    assert all(t["status"] == "ok" for t in event["tools"])
