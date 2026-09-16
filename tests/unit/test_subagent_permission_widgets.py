"""Unit tests for the subagent/permission Langfuse tiles (no network).

Pins the wiring between the event builders and the committed dashboard
snapshots, plus a repo-wide guard from the Week 3 lesson: Langfuse rejects
any non-pivot widget with more than one dimension (HTTP 400), so every
snapshot tile must carry at most one breakdown dimension.
"""

import glob
import json
from pathlib import Path

from study_agent.observability.events import (
    build_permission_event,
    build_subagent_event,
)
from study_agent.security.policy import Decision, PermissionEvent

SNAPSHOT_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "observability"
    / "langfuse-dashboard"
)

EXPECTED_TILES = {
    "Subagent runs over time": "widget-subagent-runs-over-time.json",
    "Permission decisions": "widget-permission-decisions.json",
    "Permission decisions over time": "widget-permission-decisions-over-time.json",
}


def _load(name: str) -> dict:
    with open(SNAPSHOT_DIR / name, encoding="utf-8") as f:
        return json.load(f)


def test_tiles_filter_exact_emitted_prefixes():
    """Each tile's name filter matches what its builder emits."""
    subagent_names = {
        tool["name"]
        for tool in build_subagent_event(
            subagent_name="researcher", status="success"
        )["tools"]
    }
    permission_names = {
        tool["name"]
        for tool in build_permission_event(
            [
                PermissionEvent(
                    tool="write_file",
                    arguments={},
                    decision=Decision.ALLOW,
                    executed=True,
                    source="sandbox-edit",
                )
            ]
        )["tools"]
    }
    by_file = {
        "widget-subagent-runs-over-time.json": subagent_names,
        "widget-permission-decisions.json": permission_names,
        "widget-permission-decisions-over-time.json": permission_names,
    }
    for tile_name, filename in EXPECTED_TILES.items():
        spec = _load(filename)
        assert spec["name"] == tile_name
        needles = [f["value"] for f in spec["filters"] if f["column"] == "name"]
        assert len(needles) == 1
        assert all(needles[0] in name for name in by_file[filename])


def test_denied_and_always_names_stay_inside_tile_filters():
    """Denied and session-always calls must land on the permission tiles."""
    from study_agent.security.policy import SESSION_ALWAYS_TIER, Decision, PermissionEvent

    event = build_permission_event(
        [
            PermissionEvent(
                tool="web_search", arguments={},
                decision=Decision.DENY, executed=False, source="consequential",
            ),
            PermissionEvent(
                tool="write_file", arguments={},
                decision=Decision.ALLOW, executed=True, source=SESSION_ALWAYS_TIER,
            ),
        ]
    )
    names = [tool["name"] for tool in event["tools"]]
    assert names == ["permission:denied:web_search", "permission:always:write_file"]
    for filename in (
        "widget-permission-decisions.json",
        "widget-permission-decisions-over-time.json",
    ):
        needle = next(
            f["value"] for f in _load(filename)["filters"] if f["column"] == "name"
        )
        assert all(needle in name for name in names)


def test_tiles_use_single_breakdown_dimension():
    """Timelines split ok vs fail by level; the pie splits per tool."""
    expected_dims = {
        "widget-subagent-runs-over-time.json": ["level"],
        "widget-permission-decisions.json": ["name"],
        "widget-permission-decisions-over-time.json": ["level"],
    }
    for filename, dims in expected_dims.items():
        spec = _load(filename)
        assert [d["field"] for d in spec["dimensions"]] == dims
    assert _load("widget-permission-decisions.json")["chartType"] == "PIE"


def test_no_snapshot_widget_has_multiple_dimensions_unless_pivot():
    """Repo-wide guard: Langfuse 400s non-pivot widgets with 2+ dimensions."""
    bad = []
    for path in sorted(glob.glob(str(SNAPSHOT_DIR / "widget-*.json"))):
        spec = json.load(open(path, encoding="utf-8"))
        if len(spec.get("dimensions", [])) > 1 and spec.get("chartType") != "PIVOT_TABLE":
            bad.append(Path(path).name)
    assert bad == []


def test_tiles_are_placed_on_dashboard():
    """All four tiles have a placement on the Studymate dashboard."""
    dashboard = _load("studymate-dashboard.json")
    placed = {
        p["widgetId"]
        for p in dashboard["definition"]["widgets"]
        if p.get("type") == "widget"
    }
    for filename in EXPECTED_TILES.values():
        assert _load(filename)["id"] in placed
