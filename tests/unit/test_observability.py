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
from datetime import datetime, timezone
from types import SimpleNamespace
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
    before = enabled_metrics.latest_metrics_text()
    p_allow = _series_value_or_zero(before, "studymate_permission_decisions_total", 'decision="allow",tool="write_file"')
    p_deny = _series_value_or_zero(before, "studymate_permission_decisions_total", 'decision="deny",tool="web_search"')
    sub_res = _series_value_or_zero(before, "studymate_subagent_runs_total", 'config="researcher",status="success"')
    sub_wri = _series_value_or_zero(before, "studymate_subagent_runs_total", 'config="writer",status="error"')

    enabled_metrics.observe_permission("write_file", "allow")
    enabled_metrics.observe_permission("web_search", "deny")
    enabled_metrics.observe_subagent("researcher", "success")
    enabled_metrics.observe_subagent("writer", "error")

    text = enabled_metrics.latest_metrics_text()
    assert _series_value(text, "studymate_permission_decisions_total", 'decision="allow",tool="write_file"') == p_allow + 1.0
    assert _series_value(text, "studymate_permission_decisions_total", 'decision="deny",tool="web_search"') == p_deny + 1.0
    assert _series_value(text, "studymate_subagent_runs_total", 'config="researcher",status="success"') == sub_res + 1.0
    assert _series_value(text, "studymate_subagent_runs_total", 'config="writer",status="error"') == sub_wri + 1.0


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

    before = enabled_metrics.latest_metrics_text()
    prev_not_found = _series_value_or_zero(before, "studymate_tool_calls_total", 'source="builtin",status="not_found",tool="no_such_tool"')

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
    assert _series_value(text, "studymate_tool_calls_total", 'source="builtin",status="not_found",tool="no_such_tool"') == prev_not_found + 1.0


def test_trace_event_schema_and_append(tmp_path):
    """Verify the schema-v1 event round-trips through one JSON line."""
    event = tracing.build_run_event(
        model="unit-model",
        status="success",
        run_id="abc123",
        thread_id="tid9",
        thread_name="unit-thread",
        iterations=3,
        prompt_tokens=20,
        completion_tokens=10,
        tools=[{"name": "read_file", "source": "builtin", "duration_ms": 4.5, "status": "ok"}],
        compactions=1,
    )
    assert event["schema"] == 2
    assert event["tokens"]["total"] == 30
    assert event["thread"] == {"id": "tid9", "name": "unit-thread"}
    assert event["final_response"] == ""
    assert event["messages"] == [] and event["turns"] == []

    trace_file = tmp_path / "traces.jsonl"
    assert tracing.append_trace(trace_file, event) is not None
    lines = trace_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["run_id"] == "abc123"


def test_trace_arg_truncation_with_marker():
    """Verify oversized tool arguments truncate with a marker; shorts pass through."""
    big = {"blob": "y" * 5000, "small": "ok"}
    truncated = tracing.truncate_args(big, 100)
    assert truncated["small"] == "ok"
    assert truncated["blob"].endswith("…[truncated 4900 chars]")
    assert tracing.truncate_args({"a": 1}, 10) == {"a": 1}
    assert tracing.truncate_args("x" * 50, 10).endswith("…[truncated 40 chars]")
    assert tracing.truncate_args(None, 10) is None


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
    assert calls["observation"]["name"] == "studymate-run:success (0.0s)"
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


def test_engine_trace_carries_thread_messages_and_turns(tmp_path, enabled_metrics):
    """Verify v2 event holds thread, verbatim messages, turns, final answer."""

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
        tool_calls=[ToolCall(id="c1", name="echo_tool", arguments={"big": "z" * 9000})],
        reasoning_tokens=6,
    )
    llm.add_response(content="finished", reasoning_tokens=4)
    trace_file = tmp_path / "traces.jsonl"
    obs = ObservabilityConfig(metrics_enabled=True, trace_file=str(trace_file))
    engine = AgentEngine(llm_client=llm, tool_registry=registry, observability=obs)

    result = engine.run(
        "do the thing",
        ContextManager(custom_system_prompt="SYS"),
        thread_id="tid1",
        thread_name="pizza-chat",
    )

    assert result.is_success is True
    assert result.reasoning_tokens == 10
    event = json.loads(trace_file.read_text(encoding="utf-8").strip())
    assert event["schema"] == 2
    assert event["tokens"]["reasoning"] == 10
    assert event["thread"] == {"id": "tid1", "name": "pizza-chat"}
    assert [m["role"] for m in event["messages"]] == ["system", "user", "assistant", "tool", "assistant"]
    # System prompt carries the base prompt plus main's "Current date:" anchor.
    assert event["messages"][0]["content"].startswith("SYS")
    assert len(event["turns"]) == 2
    assert event["turns"][0]["llm"]["tool_calls"][0]["name"] == "echo_tool"
    assert event["turns"][0]["llm"]["tool_calls"][0]["arguments"]["big"].endswith("…[truncated 5000 chars]")
    assert event["turns"][0]["tools"] == [{"name": "echo_tool", "observation": "echoed"}]
    assert event["turns"][1]["llm"]["content"] == "finished"
    assert event["turns"][0]["llm"]["reasoning_tokens"] == 6
    assert event["turns"][1]["llm"]["reasoning_tokens"] == 4
    assert event["final_response"] == "finished"
    assert event["duration_s"] >= 0.0


def test_truncate_messages_bounds_payload():
    """Verify per-message contents truncate with markers; shapes survive."""
    messages = [
        {"role": "system", "content": "s" * 5000},
        {"role": "user", "content": "short"},
        {"role": "tool", "content": None},
        "not-a-dict",
    ]
    truncated = tracing.truncate_messages(messages, 100)
    assert truncated[0]["content"].endswith("…[truncated 4900 chars]")
    assert truncated[0]["role"] == "system"
    assert truncated[1]["content"] == "short"
    assert truncated[2]["content"] is None
    assert truncated[3] == "not-a-dict"
    assert tracing.truncate_messages(messages, 0)[0]["content"] == "s" * 5000
    assert tracing.truncate_messages("nope", 10) == "nope"
    assert tracing.truncate_messages(None, 10) is None


def test_engine_system_prompt_extraction():
    """Verify the leading SYSTEM block is found across role shapes."""
    from study_agent.core.types import Role

    sys_msg = SimpleNamespace(role=Role.SYSTEM, content="  persona  ")
    user_msg = SimpleNamespace(role=Role.USER, content="hi")
    assert AgentEngine._system_prompt([sys_msg, user_msg]) == "  persona  "
    str_msg = SimpleNamespace(role="system", content="plain")
    assert AgentEngine._system_prompt([str_msg]) == "plain"
    assert AgentEngine._system_prompt([user_msg]) == ""
    assert AgentEngine._system_prompt([]) == ""
    assert AgentEngine._system_prompt(None) == ""
    blank = SimpleNamespace(role=Role.SYSTEM, content="   ")
    assert AgentEngine._system_prompt([blank]) == ""


def test_usage_details_mapping():
    """Verify token counts map onto input/output/total without double-billing."""
    assert tracing.build_usage_details(10, 20, 30) == {"input": 10, "output": 20, "total": 30}
    assert tracing.build_usage_details(10, 20, 0) == {"input": 10, "output": 20, "total": 30}
    assert tracing.build_usage_details() == {"input": 0, "output": 0, "total": 0}


def test_trace_event_carries_model_params_and_tools():
    """Verify the event holds model parameters + available tools for export."""
    event = tracing.build_run_event(
        model="m",
        status="success",
        model_parameters={"temperature": 0.2, "max_tokens": 2048},
        available_tools=["read_file", "echo_tool"],
    )
    assert event["model_parameters"] == {"temperature": 0.2, "max_tokens": 2048}
    assert event["available_tools"] == ["read_file", "echo_tool"]


class _FakeCM:
    """Stands in for langfuse.propagate_attributes' context manager."""

    def __init__(self, calls: Dict[str, Any], kwargs: Dict[str, Any]) -> None:
        self._calls = calls
        self._calls["propagate"] = kwargs

    def __enter__(self) -> "_FakeCM":
        self._calls["propagate_entered"] = True
        return self

    def __exit__(self, *args: Any) -> bool:
        self._calls["propagate_exited"] = True
        return False


class _FakeOtelSpan:
    """Stands in for the wrapped OTel span (records attribute stamps)."""

    def __init__(self, calls: Dict[str, Any]) -> None:
        self._calls = calls

    def set_attribute(self, key: str, value: Any) -> None:
        self._calls.setdefault("span_attrs", []).append((key, value))


class _FakeChild:
    """Stands in for Langfuse observation wrappers (span/agent/generation/tool)."""

    def __init__(self, calls: Dict[str, Any], kwargs: Dict[str, Any], record: bool = True) -> None:
        self._calls = calls
        self.kwargs = kwargs
        self._otel_span = _FakeOtelSpan(calls)
        if record:
            calls.setdefault("children", []).append(kwargs)

    def start_observation(self, **kwargs: Any) -> "_FakeChild":
        return _FakeChild(self._calls, kwargs)

    def update(self, **kwargs: Any) -> None:
        self.kwargs["updated"] = kwargs

    def end(self, **kwargs: Any) -> None:
        self._calls["ended"] = self._calls.get("ended", 0) + 1


class _FakeLangfuseClient:
    def __init__(self, calls: Dict[str, Any], **kwargs: Any) -> None:
        self._calls = calls
        calls["init"] = kwargs

    def create_trace_id(self) -> str:
        return "mock-trace-id"

    def start_observation(self, **kwargs: Any) -> _FakeChild:
        self._calls["observation"] = kwargs
        return _FakeChild(self._calls, kwargs, record=False)

    def flush(self) -> None:
        self._calls["flushed"] = True


def _install_fake_langfuse(monkeypatch, calls: Dict[str, Any]) -> None:
    """Installs fake langfuse/langfuse.types modules (no network, no SDK)."""
    fake_module = types.ModuleType("langfuse")
    fake_module.Langfuse = lambda **kwargs: _FakeLangfuseClient(calls, **kwargs)  # type: ignore[attr-defined]
    fake_module.propagate_attributes = lambda **kwargs: _FakeCM(calls, kwargs)  # type: ignore[attr-defined]
    fake_module.LangfuseOtelSpanAttributes = SimpleNamespace(TRACE_TAGS="langfuse.trace.tags")  # type: ignore[attr-defined]
    fake_types = types.ModuleType("langfuse.types")
    fake_types.TraceContext = lambda **kwargs: kwargs  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "langfuse", fake_module)
    monkeypatch.setitem(sys.modules, "langfuse.types", fake_types)
    monkeypatch.setattr(tracing, "_host_reachable", lambda host, timeout_s=3.0: True)
    # Flush runs on a daemon thread; run it inline so assertions are race-free.
    monkeypatch.setattr(tracing, "_flush_async", lambda client: calls.update(flushed=True))


def test_export_emits_generation_and_tool_children(monkeypatch):
    """Verify the fallback export nests model/usage generations + tool calls."""
    calls: Dict[str, Any] = {}
    _install_fake_langfuse(monkeypatch, calls)

    event = tracing.build_run_event(
        model="m",
        status="success",
        run_id="r1",
        duration_s=2.0,
        model_parameters={"temperature": 0.2},
        available_tools=["echo_tool"],
        messages=[{"role": "system", "content": "SYS"}],
        tools=[{"name": "echo_tool", "source": "builtin", "server": "", "status": "ok"}],
        turns=[
            {
                "n": 1,
                "llm": {
                    "content": "hi",
                    "reasoning_content": "hmm",
                    "tool_calls": [{"name": "echo_tool", "arguments": {}}],
                    "finish_reason": "tool_calls",
                    "prompt_tokens": 5,
                    "completion_tokens": 7,
                    "reasoning_tokens": 0,
                },
                "tools": [{"name": "echo_tool", "observation": "echoed"}],
            }
        ],
    )
    assert tracing.export_langfuse(event, host="h", public_key="p", secret_key="s") is True

    gen = calls["children"][0]
    assert gen["as_type"] == "generation"
    assert gen["model"] == "m"
    assert gen["usage_details"] == {"input": 5, "output": 7, "total": 12}
    assert gen["output"]["reasoning_content"] == "hmm"
    assert gen["input"]["available_tools"] == ["echo_tool"]

    tool = calls["children"][1]
    assert tool["name"] == "echo_tool"
    assert tool["as_type"] == "tool"
    assert tool["output"] == "echoed"
    assert tool["metadata"]["source"] == "builtin"
    assert tool["metadata"]["status"] == "ok"
    # Same-name run record must not duplicate the per-turn child.
    assert len(calls["children"]) == 2
    assert calls["observation"]["output"]["messages"] == [{"role": "system", "content": "SYS"}]
    assert calls["flushed"] is True


def test_live_run_span_lifecycle(monkeypatch):
    """Verify live open/update/end stamps true latency with model+usage."""
    calls: Dict[str, Any] = {}
    _install_fake_langfuse(monkeypatch, calls)

    handle = tracing.start_langfuse_run(
        host="h",
        public_key="p",
        secret_key="s",
        run_id="a" * 32,
        model="live-model",
        thread_id="tid",
        thread_name="tn",
        user_message="hello",
        model_parameters={"temperature": 0.2},
        available_tools=["echo_tool"],
        system_prompt="SYS PROMPT",
        tool_schemas=[{"type": "function"}],
        tool_choice="auto",
        injected_memory="FACTS BLOCK",
        max_chars=50,
    )
    assert handle is not None
    assert handle["max_chars"] == 50
    assert calls["propagate"]["session_id"] == "tid"
    assert calls["propagate"]["tags"] == ["tn"]
    root_input = calls["observation"]
    assert root_input["input"]["system_prompt"] == "SYS PROMPT"
    assert root_input["input"]["tool_schemas"] == [{"type": "function"}]
    assert root_input["input"]["tool_choice"] == "auto"
    assert root_input["input"]["injected_memory"] == "FACTS BLOCK"

    gen = tracing.start_langfuse_generation(
        handle,
        name="llm-turn-1",
        turn=1,
        available_tools=["echo_tool"],
        new_messages=[
            {"role": "system", "content": "s" * 100},
            {"role": "user", "content": "hi"},
        ],
    )
    assert gen is not None
    finish_time = datetime.now(timezone.utc)
    tracing.finish_langfuse_generation(
        gen,
        output={"content": "hi"},
        model="live-model",
        usage_details=tracing.build_usage_details(5, 7, 12),
        model_parameters={"temperature": 0.2},
        completion_start_time=finish_time,
    )
    child = calls["children"][0]
    assert child["as_type"] == "generation"
    assert child["updated"]["model"] == "live-model"
    assert child["updated"]["usage_details"] == {"input": 5, "output": 7, "total": 12}
    assert child["updated"]["completion_start_time"] is finish_time
    delta = child["input"]["new_messages"]
    assert [m["role"] for m in delta] == ["system", "user"]
    assert delta[0]["content"].endswith("…[truncated 50 chars]")
    assert child["input"]["compacted"] is False

    tool = tracing.start_langfuse_tool(handle["span"], name="echo_tool", input={})
    tracing.finish_langfuse_tool(tool, output="echoed", metadata={"status": "ok"})
    assert calls["children"][1]["name"] == "echo_tool"

    event = tracing.build_run_event(
        model="live-model", status="success", run_id="a" * 32,
        thread_id="tid", thread_name="tn",
        duration_s=1.25, final_response="hi",
        messages=[{"role": "system", "content": "m" * 100}],
    )
    assert tracing.finish_langfuse_run(handle, event) is True
    assert calls["observation"]["updated"]["name"] == "studymate-run:success (1.2s)"
    assert calls["observation"]["updated"]["output"]["final_response"] == "hi"
    assert calls["observation"]["updated"]["output"]["status"] == "success"
    assert ("langfuse.trace.tags", ["tn", "success"]) in calls["span_attrs"]
    assert calls["observation"]["updated"]["output"]["messages"][0]["content"].endswith(
        "…[truncated 50 chars]"
    )
    assert calls["propagate_exited"] is True
    assert calls["flushed"] is True


def test_abort_langfuse_run_closes_span(monkeypatch):
    """Verify an LLM failure still closes the live span as error."""
    calls: Dict[str, Any] = {}
    _install_fake_langfuse(monkeypatch, calls)

    handle = tracing.start_langfuse_run(
        host="h", public_key="p", secret_key="s", run_id="b" * 32, model="m"
    )
    assert calls["observation"]["input"]["injected_memory"] == ""
    assert tracing.abort_langfuse_run(handle, "boom") is True
    assert calls["observation"]["updated"]["name"] == "studymate-run:error (0.0s)"
    assert ("langfuse.trace.tags", ["error"]) in calls["span_attrs"]
    assert calls["observation"]["updated"]["level"] == "ERROR"
    assert tracing.abort_langfuse_run(None, "x") is False
    assert tracing.finish_langfuse_run(None, {}) is False


def test_export_emits_record_children_without_turns(monkeypatch):
    """Verify run-level tool records (e.g. heartbeats) become root children."""
    calls: Dict[str, Any] = {}
    _install_fake_langfuse(monkeypatch, calls)

    event = tracing.build_run_event(
        model="",
        status="success",
        run_id="r9",
        thread_id="mcp-health",
        thread_name="mcp-health",
        final_response="MCP health: 1/1 servers live",
        tools=[
            {
                "name": "mcp-health:duckduckgo-bridge",
                "source": "mcp",
                "server": "external-mcp",
                "status": "ok",
                "duration_ms": 5.0,
                "arguments": {"detail": "3 remote tools"},
            }
        ],
        turns=[],
    )
    assert tracing.export_langfuse(event, host="h", public_key="p", secret_key="s") is True
    assert len(calls["children"]) == 1
    child = calls["children"][0]
    assert child["as_type"] == "tool"
    assert child["name"] == "mcp-health:duckduckgo-bridge"
    assert child["metadata"]["server"] == "external-mcp"
    assert child["output"] == "3 remote tools"
    assert child["level"] == "DEFAULT"


def test_export_failed_records_end_as_error(monkeypatch):
    """Verify a DOWN server's heartbeat child is ERROR level, not DEFAULT."""
    calls: Dict[str, Any] = {}
    _install_fake_langfuse(monkeypatch, calls)

    event = tracing.build_run_event(
        model="",
        status="error",
        run_id="r8",
        thread_id="mcp-health",
        thread_name="mcp-health",
        final_response="MCP health: 0/1 servers live",
        tools=[
            {
                "name": "mcp-health:obsidian-vault",
                "source": "mcp",
                "server": "local-mcp",
                "status": "error",
                "duration_ms": 1.0,
                "arguments": {"detail": "ValueError: gone"},
            }
        ],
        turns=[],
        error="obsidian-vault: ValueError: gone",
    )
    assert tracing.export_langfuse(event, host="h", public_key="p", secret_key="s") is True
    assert len(calls["children"]) == 1
    assert calls["children"][0]["level"] == "ERROR"


def test_trace_tags_degrade_silently():
    """Verify tag stamping never raises on odd spans/events."""
    tracing._set_trace_tags(None, ["success"])
    tracing._set_trace_tags(object(), ["success"])
    tracing._set_trace_tags(SimpleNamespace(), [])
    assert tracing._run_tags({}) == []
    assert tracing._run_tags({"status": "max_iterations"}) == ["max_iterations"]
    assert tracing._run_tags({"status": "success", "thread": {"id": "t", "name": "tn"}}) == [
        "tn",
        "success",
    ]


def test_start_langfuse_run_none_without_backend(monkeypatch):
    """Verify live open degrades to None (post-hoc fallback) when down."""
    calls: Dict[str, Any] = {}
    _install_fake_langfuse(monkeypatch, calls)
    monkeypatch.setattr(tracing, "_host_reachable", lambda host, timeout_s=3.0: False)
    assert tracing.start_langfuse_run(host="h", public_key="p", secret_key="s") is None
    # Missing keys never touch the network either.
    monkeypatch.setattr(tracing, "_host_reachable", lambda host, timeout_s=3.0: True)
    assert tracing.start_langfuse_run(host="h") is None


def test_engine_live_langfuse_spans_per_turn(tmp_path, enabled_metrics, monkeypatch):
    """Verify the engine opens live generations/tools and finishes exactly once."""
    import study_agent.core.engine as engine_mod

    calls: Dict[str, Any] = {"generations": [], "tools": [], "finished": [], "fallback": []}

    def _fake_start(**kw: Any) -> Dict[str, Any]:
        calls["started"] = kw
        return {"span": "root", "params": kw}

    monkeypatch.setattr(engine_mod, "start_langfuse_run", _fake_start)
    monkeypatch.setattr(
        engine_mod,
        "start_langfuse_generation",
        lambda handle, **kw: calls["generations"].append(kw) or {"gen": kw},
    )
    monkeypatch.setattr(
        engine_mod,
        "finish_langfuse_generation",
        lambda span, **kw: calls.setdefault("gen_finished", []).append(kw),
    )
    monkeypatch.setattr(
        engine_mod,
        "start_langfuse_tool",
        lambda parent, **kw: calls["tools"].append(kw) or {"tool": kw},
    )
    monkeypatch.setattr(
        engine_mod,
        "finish_langfuse_tool",
        lambda span, **kw: calls.setdefault("tool_finished", []).append(kw),
    )
    monkeypatch.setattr(
        engine_mod,
        "finish_langfuse_run",
        lambda handle, event: calls["finished"].append(event) or True,
    )
    monkeypatch.setattr(
        engine_mod,
        "export_langfuse_async",
        lambda *a, **k: calls["fallback"].append(True),
    )

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
        reasoning_content="thinking-1",
        reasoning_tokens=6,
    )
    llm.add_response(content="finished", reasoning_tokens=4)
    trace_file = tmp_path / "traces.jsonl"
    obs = ObservabilityConfig(
        metrics_enabled=True, langfuse_enabled=True, trace_file=str(trace_file)
    )
    engine = AgentEngine(llm_client=llm, tool_registry=registry, observability=obs)

    ctx = ContextManager()
    ctx.inject_system_note("MEMO")
    result = engine.run("do the thing", ctx, thread_id="t1", thread_name="tn")

    assert result.is_success is True
    assert calls["started"]["injected_memory"] == "MEMO"
    assert len(calls["generations"]) == 2
    assert len(calls["tools"]) == 1
    assert len(calls["gen_finished"]) == 2
    assert calls["gen_finished"][0]["usage_details"] == {"input": 10, "output": 10, "total": 20}
    assert calls["gen_finished"][0]["completion_start_time"] is not None
    assert len(calls["finished"]) == 1
    assert calls["fallback"] == []
    # Full starting request rides once on the root span …
    start_params = calls.get("started", {})
    assert isinstance(start_params.get("system_prompt"), str) and start_params["system_prompt"]
    assert start_params["tool_choice"] == "auto"
    assert any(
        (s.get("function", {}) or {}).get("name") == "echo_tool"
        for s in start_params.get("tool_schemas", [])
        if isinstance(s, dict)
    )
    # … while each generation carries only its delta (no duplication).
    first_delta = calls["generations"][0]["new_messages"]
    assert [m["role"] for m in first_delta] == ["system", "user"]
    second_delta = calls["generations"][1]["new_messages"]
    assert [m["role"] for m in second_delta] == ["assistant", "tool"]
    assert calls["generations"][0]["compacted"] is False
    event = calls["finished"][0]
    assert event["turns"][0]["llm"]["reasoning_content"] == "thinking-1"
    assert event["turns"][0]["llm"]["latency_s"] >= 0.0
    assert event["available_tools"] == ["echo_tool"]
    assert calls["tool_finished"][0]["metadata"]["source"] == "builtin"
    assert calls["tool_finished"][0]["metadata"]["server"] == ""
    assert event["tools"][0]["server"] == ""


def test_engine_error_aborts_live_span(tmp_path, enabled_metrics, monkeypatch):
    """Verify an LLM failure closes the live span and still raises."""
    import study_agent.core.engine as engine_mod

    calls: Dict[str, Any] = {"aborted": [], "fallback": []}
    monkeypatch.setattr(engine_mod, "start_langfuse_run", lambda **kw: {"span": "root"})
    monkeypatch.setattr(engine_mod, "start_langfuse_generation", lambda handle, **kw: {"gen": 1})
    monkeypatch.setattr(engine_mod, "finish_langfuse_generation", lambda span, **kw: None)
    monkeypatch.setattr(
        engine_mod, "abort_langfuse_run", lambda handle, err: calls["aborted"].append(err) or True
    )
    monkeypatch.setattr(
        engine_mod,
        "export_langfuse_async",
        lambda *a, **k: calls["fallback"].append(True),
    )

    class _Boom(MockLLMClient):
        def chat(self, messages, tools=None):  # type: ignore[override]
            raise TimeoutError("slow")

    trace_file = tmp_path / "traces.jsonl"
    obs = ObservabilityConfig(
        metrics_enabled=True, langfuse_enabled=True, trace_file=str(trace_file)
    )
    engine = AgentEngine(llm_client=_Boom(), tool_registry=ToolRegistry(), observability=obs)

    with pytest.raises(TimeoutError):
        engine.run("boom", ContextManager())
    assert calls["aborted"] == ["slow"]
    assert calls["fallback"] == []


def test_engine_tool_server_labels(tmp_path, enabled_metrics, monkeypatch):
    """Verify tool spans carry source + server for MCP health widgets."""
    import study_agent.core.engine as engine_mod

    calls: Dict[str, Any] = {"finished": []}
    monkeypatch.setattr(engine_mod, "start_langfuse_run", lambda **kw: {"span": "root"})
    monkeypatch.setattr(engine_mod, "start_langfuse_generation", lambda handle, **kw: {"gen": 1})
    monkeypatch.setattr(engine_mod, "finish_langfuse_generation", lambda span, **kw: None)
    monkeypatch.setattr(engine_mod, "start_langfuse_tool", lambda parent, **kw: {"tool": 1})
    monkeypatch.setattr(
        engine_mod,
        "finish_langfuse_tool",
        lambda span, **kw: calls.setdefault("tool_finished", []).append(kw),
    )
    monkeypatch.setattr(
        engine_mod, "finish_langfuse_run", lambda handle, event: calls["finished"].append(event)
    )

    class _BridgeTool(BaseTool):
        """Mimics ExternalMCPTool (remote_name, mcp module) without the import."""

        remote_name = "search"

        @property
        def name(self) -> str:
            return "web_search"

        @property
        def description(self) -> str:
            return "Search."

        @property
        def parameters(self) -> Dict[str, Any]:
            return {"type": "object", "properties": {}}

        def execute(self, **kwargs: Any) -> str:
            return "hits"

    _BridgeTool.__module__ = "study_agent.tools.mcp.tools"

    class _VaultTool(BaseTool):
        @property
        def name(self) -> str:
            return "obsidian_list_notes"

        @property
        def description(self) -> str:
            return "List."

        @property
        def parameters(self) -> Dict[str, Any]:
            return {"type": "object", "properties": {}}

        def execute(self, **kwargs: Any) -> str:
            return "notes"

    _VaultTool.__module__ = "study_agent.tools.mcp.tools"

    registry = ToolRegistry()
    registry.register(_BridgeTool())
    registry.register(_VaultTool())
    llm = MockLLMClient()
    llm.add_response(
        content="", tool_calls=[ToolCall(id="c1", name="web_search", arguments={})]
    )
    llm.add_response(
        content="", tool_calls=[ToolCall(id="c2", name="obsidian_list_notes", arguments={})]
    )
    llm.add_response(content="done")
    trace_file = tmp_path / "traces.jsonl"
    obs = ObservabilityConfig(
        metrics_enabled=True, langfuse_enabled=True, trace_file=str(trace_file)
    )
    engine = AgentEngine(llm_client=llm, tool_registry=registry, observability=obs)

    assert engine.run("search stuff", ContextManager()).is_success is True
    finished = calls["tool_finished"]
    assert [(f["metadata"] or {}).get("server") for f in finished] == [
        "external-mcp",
        "local-mcp",
    ]
    assert {(f["metadata"] or {}).get("source") for f in finished} == {"mcp"}
    assert [t["server"] for t in calls["finished"][0]["tools"]] == [
        "external-mcp",
        "local-mcp",
    ]
