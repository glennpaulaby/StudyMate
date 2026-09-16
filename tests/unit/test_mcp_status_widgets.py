"""Unit tests for the MCP status-over-time Langfuse tile (no network).

Pins the wiring between the heartbeat emitter and the committed dashboard
snapshot: the single timeline tile captures failed heartbeat ticks
(``mcp-health:*`` + ``ERROR``) split by server, so the right edge reads as
now-state — empty means all UP. Only pivot tables may carry two dimensions
(Langfuse API 400s otherwise), hence the single ``name`` breakdown with the
outcome pushed into the filter. Per-server cumulative boards are gone: a
flapping server must never appear UP and DOWN at once anymore.
"""

import json
from pathlib import Path

from study_agent.observability.health import ProbeResult, build_heartbeat_event

SNAPSHOT_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "observability"
    / "langfuse-dashboard"
)

TILE_FILE = "widget-mcp-status-over-time.json"
TILE_NAME = "MCP servers status over time"

REMOVED_TILES = [
    "widget-mcp-servers-up.json",
    "widget-mcp-servers-down.json",
    "widget-mcp-duckduckgo-status.json",
    "widget-mcp-obsidian-status.json",
]


def _load(name: str) -> dict:
    with open(SNAPSHOT_DIR / name, encoding="utf-8") as f:
        return json.load(f)


def test_status_tile_captures_all_heartbeat_observations():
    """The tile's filter matches every observation name the heartbeat emits."""
    event = build_heartbeat_event(
        [
            ProbeResult(server="external-mcp", label="duckduckgo-bridge", ok=True),
            ProbeResult(server="local-mcp", label="obsidian-vault", ok=False),
        ]
    )
    emitted = [tool["name"] for tool in event["tools"]]
    assert len(emitted) == 2
    spec = _load(TILE_FILE)
    assert spec["name"] == TILE_NAME
    name_filters = [f for f in spec["filters"] if f["column"] == "name"]
    assert len(name_filters) == 1
    needle = name_filters[0]["value"]
    assert all(needle in name for name in emitted)


def test_status_tile_splits_servers_over_time_failures_only():
    """One timeline of failed ticks per server — right edge reads as now."""
    spec = _load(TILE_FILE)
    assert spec["view"] == "observations"
    assert [d["field"] for d in spec["dimensions"]] == ["name"]
    levels = [f for f in spec["filters"] if f["column"] == "level"]
    assert len(levels) == 1 and levels[0]["value"] == "ERROR"
    assert spec["chartType"] == "BAR_TIME_SERIES"


def test_status_tile_is_placed_on_dashboard():
    """The timeline tile has a placement on the Studymate dashboard."""
    dashboard = _load("studymate-dashboard.json")
    placed = {
        p["widgetId"]
        for p in dashboard["definition"]["widgets"]
        if p.get("type") == "widget"
    }
    assert _load(TILE_FILE)["id"] in placed


def test_old_per_server_and_cumulative_boards_are_gone():
    """No cumulative or per-server snapshot boards remain to contradict it."""
    for filename in REMOVED_TILES:
        assert not (SNAPSHOT_DIR / filename).exists()
    dashboard = _load("studymate-dashboard.json")
    blob = json.dumps(dashboard)
    assert "MCP servers UP" not in blob
    assert "MCP servers DOWN" not in blob
    assert "DuckDuckGo MCP Server" not in blob
    assert "Obsidian MCP Server" not in blob
