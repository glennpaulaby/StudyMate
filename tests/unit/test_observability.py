"""Unit tests for the Week 3 Task 1 observability layer.

Covers disabled-mode no-ops, metric increments + labels, the JSONL trace
schema, best-effort Langfuse export, the teammate contract hooks, and one
scripted engine integration. Uses a high test-only metrics port so the
exporter never collides with the default 8000.
"""

import json
import re
import sys
import types
from typing import Any, Dict

import pytest

from study_agent.config.settings import ObservabilityConfig
from study_agent.core.context import ContextManager
from study_agent.core.engine import AgentEngine
from study_agent.core.types import ToolCall
from study_agent.llm.mock import MockLLMClient
from study_agent.observability import metrics, tracing
from study_agent.tools.base import BaseTool, ToolRegistry, tool_source

TEST_METRICS_PORT = 18123


def _series_value(text: str, name: str, labels: str) -> float:
    """Extracts one exposition sample value (raises AssertionError if absent)."""
    pattern = re.compile(rf"^{re.escape(name)}\{{{re.escape(labels)}\}} ([0-9.e+-]+)$", re.M)
    match = pattern.search(text)
    assert match, f"series {name}{{{labels}}} missing from exposition"
    return float(match.group(1))


def _series_value_or_zero(text: str, name: str, labels: str) -> float:
    """Same as _series_value but tolerates a not-yet-observed sample."""
    try:
        return _series_value(text, name, labels)
    except AssertionError:
        return 0.0


@pytest.fixture(scope="module")
def enabled_metrics():
    """Starts the test exporter once per module (idempotent by design)."""
    assert metrics.init_metrics(True, host="127.0.0.1", port=TEST_METRICS_PORT) is True
    assert metrics.is_enabled() is True
    return metrics


def test_disabled_mode_is_noop(monkeypatch):
    """Verify helpers never raise and emit nothing when the exporter is off."""
    monkeypatch.setattr(metrics, "_enabled", False)
    metrics.observe_run("success", 0.1, 1)
    metrics.observe_llm("m", "ok", 0.01, 5, 5)
    metrics.observe_tool("read_file", "builtin", "ok", 0.01)
    metrics.observe_memory("store_fact", "created")
    metrics.observe_compaction("threshold")
    metrics.observe_permission("write_file", "allow")
    metrics.observe_subagent("researcher", "success")
    with metrics.track_active_run():
        pass
    assert metrics.latest_metrics_text() == ""


def test_run_llm_tool_memory_compaction_series(enabled_metrics):
    """Verify increments land on the frozen series with correct labels."""
    before = enabled_metrics.latest_metrics_text()
    prev_runs = _series_value_or_zero(before, "studymate_agent_runs_total", 'status="success"')

    enabled_metrics.observe_run("success", 0.25, iterations=2)
    enabled_metrics.observe_llm("unit-model", "ok", 0.05, 11, 7)
    enabled_metrics.observe_tool("read_file", "builtin", "ok", 0.02)
    enabled_metrics.observe_tool("web_search", "mcp", "error", 0.4)
    enabled_metrics.observe_memory("store_fact", "deduplicated")
    enabled_metrics.observe_compaction("threshold")

    text = enabled_metrics.latest_metrics_text()
    assert _series_value(text, "studymate_agent_runs_total", 'status="success"') == prev_runs + 1.0
    assert _series_value(text, "studymate_llm_requests_total", 'model="unit-model",status="ok"') == 1.0
    assert _series_value(text, "studymate_llm_tokens_total", 'model="unit-model",type="prompt"') == 11.0
    assert _series_value(text, "studymate_llm_tokens_total", 'model="unit-model",type="completion"') == 7.0
    assert _series_value(text, "studymate_tool_calls_total", 'source="builtin",status="ok",tool="read_file"') >= 1.0
    assert _series_value(text, "studymate_tool_calls_total", 'source="mcp",status="error",tool="web_search"') == 1.0
    assert _series_value(text, "studymate_memory_ops_total", 'op="store_fact",result="deduplicated"') >= 1.0
    assert _series_value(text, "studymate_compactions_total", 'trigger="threshold"') >= 1.0


def test_teammate_contract_hooks(enabled_metrics):
    """Verify permission/sub-agent owners emit into the frozen contract names."""
    enabled_metrics.observe_permission("write_file", "allow")
    enabled_metrics.observe_permission("web_search", "deny")
    enabled_metrics.observe_subagent("researcher", "success")
    enabled_metrics.observe_subagent("writer", "error")

    text = enabled_metrics.latest_metrics_text()
    assert _series_value(text, "studymate_permission_decisions_total", 'decision="allow",tool="write_file"') == 1.0
    assert _series_value(text, "studymate_permission_decisions_total", 'decision="deny",tool="web_search"') == 1.0
    assert _series_value(text, "studymate_subagent_runs_total", 'config="researcher",status="success"') == 1.0
    assert _series_value(text, "studymate_subagent_runs_total", 'config="writer",status="error"') == 1.0


def test_tool_source_tiers():
    """Verify module-based source inference (builtin|mcp|memory)."""

    class _FsTool(BaseTool):
        @property
        def name(self) -> str:
            return "t"

        @property
        def description(self) -> str:
            return "d"

        @property
        def parameters(self) -> Dict[str, Any]:
            return {}

        def execute(self, **kwargs: Any) -> str:
            return "ok"

    assert tool_source(_FsTool()) == "builtin"
    mcp_like = _FsTool()
    type(mcp_like).__module__ = "study_agent.tools.mcp.tools"
    assert tool_source(mcp_like) == "mcp"
    mem_like = _FsTool()
    type(mem_like).__module__ = "study_agent.memory.tools"
    assert tool_source(mem_like) == "memory"


def test_registry_execute_observes_statuses(tmp_path, enabled_metrics):
    """Verify the registry choke point labels ok/error/not_found."""
    from study_agent.config.settings import FilesystemToolConfig
    from study_agent.tools.base import ToolRegistry
    from study_agent.tools.filesystem import register_filesystem_tools

    workspace = tmp_path / "workspace"
    (workspace / "artifacts").mkdir(parents=True)
    registry = ToolRegistry()
    register_filesystem_tools(
        registry,
        base_dir=workspace,
        config=FilesystemToolConfig(
            allowed_read_paths=[str(workspace)],
            allowed_write_paths=[str(workspace / "artifacts")],
        ),
    )
    assert "artifacts" in registry.execute("list_files", {"path": "."})
    assert registry.execute("no_such_tool", {}).startswith("Error")

    text = enabled_metrics.latest_metrics_text()
    assert _series_value(text, "studymate_tool_calls_total", 'source="builtin",status="ok",tool="list_files"') >= 1.0
    assert _series_value(text, "studymate_tool_calls_total", 'source="builtin",status="not_found",tool="no_such_tool"') == 1.0


def test_trace_event_schema_and_append(tmp_path):
    """Verify the schema-v1 event round-trips through one JSON line."""
    event = tracing.build_run_event(
        model="unit-model",
        status="success",
        run_id="abc123",
        thread="unit-thread",
        iterations=3,
        prompt_tokens=20,
        completion_tokens=10,
        tools=[{"name": "read_file", "source": "builtin", "duration_ms": 4.5, "status": "ok"}],
        compactions=1,
    )
    assert event["schema"] == 1
    assert event["tokens"]["total"] == 30

    trace_file = tmp_path / "traces.jsonl"
    assert tracing.append_trace(trace_file, event) is not None
    lines = trace_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["run_id"] == "abc123"


def test_trace_append_failure_returns_none(tmp_path):
    """Verify an unwritable path degrades to None instead of raising."""
    assert tracing.append_trace(tmp_path, {"run_id": "x"}) is None


def test_langfuse_export_skipped_without_keys():
    """Verify export is a silent no-op when keys are absent."""
    event = tracing.build_run_event(model="m", status="ok")
    assert tracing.export_langfuse(event, host="http://localhost:3000") is False


def test_langfuse_export_skipped_without_sdk(monkeypatch):
    """Verify export degrades when the SDK is uninstallable."""
    monkeypatch.setitem(sys.modules, "langfuse", None)
    event = tracing.build_run_event(model="m", status="ok")
    assert tracing.export_langfuse(event, host="h", public_key="p", secret_key="s") is False


def test_langfuse_export_unreachable_host_fails_fast():
    """Verify a down server returns False quickly instead of timing out flush."""
    import time

    event = tracing.build_run_event(model="m", status="ok")
    start = time.perf_counter()
    # Nothing listens on port 1 -> connection refused, no retry storm.
    assert tracing.export_langfuse(event, host="http://127.0.0.1:1", public_key="p", secret_key="s") is False
    assert time.perf_counter() - start < 15.0


def test_otel_loggers_quieted_on_export(monkeypatch):
    """Verify the OTel SDK spam is silenced so it can't corrupt the REPL prompt."""
    import logging

    fake_module = types.ModuleType("langfuse")
    fake_module.Langfuse = None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "langfuse", fake_module)

    event = tracing.build_run_event(model="m", status="ok")
    assert tracing.export_langfuse(event, host="h", public_key="p", secret_key="s") is False
    for name in (
        "opentelemetry",
        "opentelemetry.exporter.otlp",
        "opentelemetry.sdk.trace.export",
    ):
        assert logging.getLogger(name).level == logging.CRITICAL


def test_langfuse_export_success_mocked(monkeypatch):
    """Verify the happy path posts one observation and flushes (SDK mocked)."""
    calls: Dict[str, Any] = {}

    class _FakeSpan:
        def end(self, **kwargs: Any) -> None:
            calls["ended"] = True

    class _FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            calls["init"] = kwargs

        def create_trace_id(self) -> str:
            return "mock-trace-id"

        def start_observation(self, **kwargs: Any) -> _FakeSpan:
            calls["observation"] = kwargs
            return _FakeSpan()

        def flush(self) -> None:
            calls["flushed"] = True

    fake_module = types.ModuleType("langfuse")
    fake_module.Langfuse = _FakeClient  # type: ignore[attr-defined]
    fake_types = types.ModuleType("langfuse.types")
    fake_types.TraceContext = lambda **kwargs: kwargs  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "langfuse", fake_module)
    monkeypatch.setitem(sys.modules, "langfuse.types", fake_types)
    monkeypatch.setattr(tracing, "_host_reachable", lambda host, timeout_s=3.0: True)

    event = tracing.build_run_event(model="m", status="success", run_id="r1")
    assert tracing.export_langfuse(event, host="h", public_key="p", secret_key="s") is True
    assert calls["observation"]["name"] == "studymate-run:success"
    assert calls["ended"] is True
    assert calls["flushed"] is True


def test_langfuse_export_failure_returns_false(monkeypatch):
    """Verify SDK blowups surface as False, never raise."""
    class _BoomClient:
        def __init__(self, **kwargs: Any) -> None:
            raise ConnectionError("down")

    fake_module = types.ModuleType("langfuse")
    fake_module.Langfuse = _BoomClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "langfuse", fake_module)

    event = tracing.build_run_event(model="m", status="ok")
    assert tracing.export_langfuse(event, host="h", public_key="p", secret_key="s") is False


def test_engine_run_emits_metrics_and_trace(tmp_path, enabled_metrics):
    """Verify one scripted run produces series plus exactly one trace line."""

    class _EchoTool(BaseTool):
        @property
        def name(self) -> str:
            return "echo_tool"

        @property
        def description(self) -> str:
            return "Echoes back."

        @property
        def parameters(self) -> Dict[str, Any]:
            return {"type": "object", "properties": {}}

        def execute(self, **kwargs: Any) -> str:
            return "echoed"

    registry = ToolRegistry()
    registry.register(_EchoTool())
    llm = MockLLMClient()
    llm.add_response(
        content="",
        tool_calls=[ToolCall(id="c1", name="echo_tool", arguments={})],
    )
    llm.add_response(content="finished")
    trace_file = tmp_path / "traces.jsonl"
    obs = ObservabilityConfig(metrics_enabled=True, trace_file=str(trace_file))
    engine = AgentEngine(llm_client=llm, tool_registry=registry, observability=obs)

    result = engine.run("do the thing", ContextManager())

    assert result.is_success is True
    assert result.tool_calls_count == 1
    text = enabled_metrics.latest_metrics_text()
    assert "studymate_agent_runs_total" in text
    assert _series_value(text, "studymate_tool_calls_total", 'source="builtin",status="ok",tool="echo_tool"') >= 1.0
    lines = trace_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["status"] == "success"
    assert event["tools"][0]["name"] == "echo_tool"
