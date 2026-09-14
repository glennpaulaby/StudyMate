"""Prometheus metrics for StudyMate (Week 3 Task 1).

All series use the ``studymate_`` prefix. The ``prometheus_client`` import is
lazy with a no-op fallback so offline installs and ``MockLLMClient`` tests
keep working without the dependency.

Cross-team contract: permission and sub-agent owners emit into
``observe_permission`` / ``observe_subagent`` — never touch the client
directly, so series names stay frozen.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Dict, Iterator, Optional

logger = logging.getLogger(__name__)

try:
    from prometheus_client import Counter, Gauge, Histogram, start_http_server

    _PROM_AVAILABLE = True
except ImportError:  # pragma: no cover - offline/minimal installs
    Counter = Gauge = Histogram = None  # type: ignore[assignment,misc]
    start_http_server = None  # type: ignore[assignment]
    _PROM_AVAILABLE = False

PREFIX = "studymate"

# Frozen series called out in course material/week 3 plan.md Phase 1.
_RUN_STATUSES = ("success", "max_iterations", "error", "empty")
_COMPONENTS = ("total", "llm", "tool", "memory", "compaction")
_LLM_STATUSES = ("ok", "auth", "timeout", "connection", "api_error", "overflow")
_TOKEN_TYPES = ("prompt", "completion")
_TOOL_SOURCES = ("builtin", "mcp", "memory")
_TOOL_STATUSES = ("ok", "error", "not_found")

_metrics: Dict[str, object] = {}
_lock = threading.Lock()
_enabled = False
_server_started = False


def metrics_available() -> bool:
    """Whether the Prometheus client library is importable."""
    return _PROM_AVAILABLE


def is_enabled() -> bool:
    """Whether metrics collection was switched on via ``init_metrics``."""
    return _enabled


def _live() -> bool:
    """True only when collection was switched on (disabled mode is a no-op)."""
    return _enabled and _PROM_AVAILABLE


def _get(name: str) -> Optional[object]:
    return _metrics.get(name) if _live() else None


def init_metrics(enabled: bool, host: str = "127.0.0.1", port: int = 8000) -> bool:
    """Creates series and starts the ``/metrics`` HTTP server once.

    Returns True only when the exporter is actually serving. Safe to call
    repeatedly; safe to call when disabled or when the client is missing
    (both are silent no-ops returning False).
    """
    global _enabled, _server_started
    if not enabled or not _PROM_AVAILABLE:
        _enabled = False
        return False
    with _lock:
        if not _metrics:
            _metrics["agent_runs"] = Counter(
                f"{PREFIX}_agent_runs_total", "Agent runs by terminal status.", ["status"]
            )
            _metrics["agent_active"] = Gauge(
                f"{PREFIX}_agent_active_runs", "Currently executing agent runs."
            )
            _metrics["agent_duration"] = Histogram(
                f"{PREFIX}_agent_run_duration_seconds",
                "Agent run and component latency in seconds.",
                ["component"],
            )
            _metrics["agent_iterations"] = Counter(
                f"{PREFIX}_agent_iterations_total", "ReAct loop iterations executed."
            )
            _metrics["llm_requests"] = Counter(
                f"{PREFIX}_llm_requests_total", "LLM chat calls by model and status.", ["model", "status"]
            )
            _metrics["llm_latency"] = Histogram(
                f"{PREFIX}_llm_latency_seconds", "LLM chat call latency in seconds.", ["model"]
            )
            _metrics["llm_tokens"] = Counter(
                f"{PREFIX}_llm_tokens_total",
                "LLM token consumption by model and type (reasoning is a "
                "breakout subset of completion when the provider reports it).",
                ["model", "type"],
            )
            _metrics["tool_calls"] = Counter(
                f"{PREFIX}_tool_calls_total",
                "Tool executions by tool, source and status.",
                ["tool", "source", "status"],
            )
            _metrics["tool_duration"] = Histogram(
                f"{PREFIX}_tool_duration_seconds",
                "Tool execution latency in seconds.",
                ["tool", "source"],
            )
            _metrics["memory_ops"] = Counter(
                f"{PREFIX}_memory_ops_total",
                "Memory operations by operation and result.",
                ["op", "result"],
            )
            _metrics["compactions"] = Counter(
                f"{PREFIX}_compactions_total", "Context compactions by trigger.", ["trigger"]
            )
            _metrics["permission_decisions"] = Counter(
                f"{PREFIX}_permission_decisions_total",
                "Permission decisions by tool and decision.",
                ["tool", "decision"],
            )
            _metrics["subagent_runs"] = Counter(
                f"{PREFIX}_subagent_runs_total",
                "Sub-agent runs by config and status.",
                ["config", "status"],
            )
        if not _server_started:
            assert start_http_server is not None
            start_http_server(port, addr=host)
            _server_started = True
            logger.info(f"Prometheus metrics serving on {host}:{port}/metrics")
        _enabled = True
    return True


def latest_metrics_text() -> str:
    """Renders current series in Prometheus exposition format ("" when off)."""
    if not _enabled or not _PROM_AVAILABLE:
        return ""
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    _ = CONTENT_TYPE_LATEST
    return generate_latest().decode("utf-8")


@contextmanager
def track_active_run() -> Iterator[None]:
    """Increments the active-runs gauge for the wrapped execution."""
    gauge = _get("agent_active")
    if gauge is not None:
        gauge.inc()  # type: ignore[union-attr]
    try:
        yield
    finally:
        if gauge is not None:
            gauge.dec()  # type: ignore[union-attr]


def observe_run(status: str, duration_s: float, iterations: int = 0) -> None:
    """Records a finished agent run (status + total latency + iterations)."""
    runs = _get("agent_runs")
    duration = _get("agent_duration")
    iters = _get("agent_iterations")
    if runs is None or duration is None or iters is None:
        return
    runs.labels(status=status).inc()  # type: ignore[union-attr]
    duration.labels(component="total").observe(duration_s)  # type: ignore[union-attr]
    if iterations:
        iters.inc(iterations)  # type: ignore[union-attr]


def observe_component_latency(component: str, duration_s: float) -> None:
    """Records latency of one run component (llm|tool|memory|compaction)."""
    duration = _get("agent_duration")
    if duration is None:
        return
    duration.labels(component=component).observe(duration_s)  # type: ignore[union-attr]


def observe_llm(
    model: str,
    status: str = "ok",
    latency_s: float = 0.0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    reasoning_tokens: int = 0,
) -> None:
    """Records one LLM chat call (count + latency + token consumption)."""
    requests = _get("llm_requests")
    latency = _get("llm_latency")
    tokens = _get("llm_tokens")
    if requests is None or latency is None or tokens is None:
        return
    requests.labels(model=model, status=status).inc()  # type: ignore[union-attr]
    latency.labels(model=model).observe(latency_s)  # type: ignore[union-attr]
    if prompt_tokens:
        tokens.labels(model=model, type="prompt").inc(prompt_tokens)  # type: ignore[union-attr]
    if completion_tokens:
        tokens.labels(model=model, type="completion").inc(completion_tokens)  # type: ignore[union-attr]
    if reasoning_tokens:
        tokens.labels(model=model, type="reasoning").inc(reasoning_tokens)  # type: ignore[union-attr]


def observe_tool(tool: str, source: str = "builtin", status: str = "ok", duration_s: float = 0.0) -> None:
    """Records one tool execution (count + latency)."""
    calls = _get("tool_calls")
    duration = _get("tool_duration")
    if calls is None or duration is None:
        return
    calls.labels(tool=tool, source=source, status=status).inc()  # type: ignore[union-attr]
    duration.labels(tool=tool, source=source).observe(duration_s)  # type: ignore[union-attr]


def observe_memory(op: str, result: str = "ok") -> None:
    """Records one memory operation (store/recall/thread/message/archive)."""
    ops = _get("memory_ops")
    if ops is None:
        return
    ops.labels(op=op, result=result).inc()  # type: ignore[union-attr]


def observe_compaction(trigger: str) -> None:
    """Records one context compaction by trigger (threshold|overflow|manual)."""
    compactions = _get("compactions")
    if compactions is None:
        return
    compactions.labels(trigger=trigger).inc()  # type: ignore[union-attr]


def observe_permission(tool: str, decision: str) -> None:
    """Contract hook for the permissions owner (allow|deny|confirm)."""
    decisions = _get("permission_decisions")
    if decisions is None:
        return
    decisions.labels(tool=tool, decision=decision).inc()  # type: ignore[union-attr]


def observe_subagent(config: str, status: str) -> None:
    """Contract hook for the sub-agents owner (config + terminal status)."""
    runs = _get("subagent_runs")
    if runs is None:
        return
    runs.labels(config=config, status=status).inc()  # type: ignore[union-attr]
