"""Unit tests for the MCP health heartbeat (probes + fan-out, no network)."""

import asyncio
from typing import Any, Dict, List

import pytest

from study_agent.observability import health, metrics
from study_agent.observability.health import (
    HealthHeartbeat,
    ProbeResult,
    build_heartbeat_event,
    probe_external_bridge,
    probe_obsidian_vault,
)

TEST_METRICS_PORT = 18126


@pytest.fixture(scope="module")
def enabled_metrics():
    """Starts the test exporter once per module (shares global registry)."""
    assert metrics.init_metrics(True, host="127.0.0.1", port=TEST_METRICS_PORT) is True
    return metrics


def _ok_bridge(monkeypatch, names: List[str] | None = None):
    """Stubs the MCP session listing with scripted remote tools."""

    async def _fake(command: str, args: List[str], env: Dict[str, str], timeout_s: float):
        return names if names is not None else ["search", "fetch_content"]

    monkeypatch.setattr(health, "_list_bridge_tools", _fake)


def test_external_bridge_probe_ok(monkeypatch):
    """Verify a listing bridge reports live with tool inventory."""
    _ok_bridge(monkeypatch)
    result = probe_external_bridge()
    assert result.ok is True
    assert result.server == "external-mcp"
    assert "2 remote tools" in result.detail
    assert result.latency_s >= 0.0


def test_external_bridge_probe_failure(monkeypatch):
    """Verify server blowups degrade to ok=False with a capped detail."""

    async def _boom(command: str, args: List[str], env: Dict[str, str], timeout_s: float):
        raise ConnectionError("gateway down")

    monkeypatch.setattr(health, "_list_bridge_tools", _boom)
    result = probe_external_bridge()
    assert result.ok is False
    assert "ConnectionError" in result.detail


def test_external_bridge_probe_timeout(monkeypatch):
    """Verify a hung server degrades instead of hanging the tick."""

    async def _hang(command: str, args: List[str], env: Dict[str, str], timeout_s: float):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(health, "_list_bridge_tools", _hang)
    result = probe_external_bridge(timeout_s=0.01)
    assert result.ok is False
    assert "TimeoutError" in result.detail


def test_obsidian_vault_probe_ok_and_missing(tmp_path):
    """Verify a readable vault passes and a missing one fails cleanly."""
    (tmp_path / "note.md").write_text("# hi", encoding="utf-8")
    ok_result = probe_obsidian_vault(tmp_path)
    assert ok_result.ok is True
    assert ok_result.server == "local-mcp"
    assert "vault ready" in ok_result.detail

    missing = probe_obsidian_vault(tmp_path / "nope")
    assert missing.ok is False
    assert "ValueError" in missing.detail


def test_heartbeat_event_shape():
    """Verify mixed results build an error heartbeat with mcp-health tools."""
    results = [
        ProbeResult(server="external-mcp", label="duckduckgo-bridge", ok=True, latency_s=0.5),
        ProbeResult(server="local-mcp", label="obsidian-vault", ok=False, latency_s=0.1,
                    detail="ValueError: gone"),
    ]
    event = build_heartbeat_event(results)
    assert event["status"] == "error"
    assert event["thread"] == {"id": "mcp-health", "name": "mcp-health"}
    assert "1/2 servers live" in event["final_response"]
    assert [t["name"] for t in event["tools"]] == [
        "mcp-health:duckduckgo-bridge",
        "mcp-health:obsidian-vault",
    ]
    assert [t["status"] for t in event["tools"]] == ["ok", "error"]
    assert event["error"] != ""

    good = build_heartbeat_event(results[:1])
    assert good["status"] == "success"
    assert good["error"] == ""

    empty = build_heartbeat_event([])
    assert empty["status"] == "error"
    assert empty["tools"] == []


def test_heartbeat_tick_fans_out(enabled_metrics):
    """Verify one tick records gauges and fires the callback exactly once."""
    seen: List[List[ProbeResult]] = []

    def _boom() -> ProbeResult:
        raise RuntimeError("probe contract breach")

    hb = HealthHeartbeat(
        probes=[
            lambda: ProbeResult(server="external-mcp", label="a", ok=True, latency_s=0.2),
            _boom,
        ],
        interval_s=60.0,
        on_result=seen.append,
    )
    results = hb.tick()
    assert [r.ok for r in results] == [True, False]
    assert results[1].server == "unknown"
    assert len(seen) == 1 and seen[0] == results

    text = enabled_metrics.latest_metrics_text()
    assert 'studymate_mcp_up{server="external-mcp"} 1.0' in text
    assert 'studymate_mcp_up{server="unknown"} 0.0' in text
    assert 'studymate_mcp_probe_duration_seconds{server="external-mcp"} 0.2' in text


def test_heartbeat_start_stop_idempotent():
    """Verify the scheduler starts once and stops cleanly."""
    hb = HealthHeartbeat(probes=[], interval_s=60.0)
    assert hb.start() is hb
    assert hb.start() is hb
    hb.stop()
    assert hb._stop.is_set()


def test_mcp_health_disabled_mode_is_noop(monkeypatch):
    """Verify gauge writes never raise and emit nothing when off."""
    monkeypatch.setattr(metrics, "_enabled", False)
    metrics.observe_mcp_health("external-mcp", True, 0.1)
    assert metrics.latest_metrics_text() == ""
