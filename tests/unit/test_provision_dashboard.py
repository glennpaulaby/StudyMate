"""Unit tests for the Langfuse dashboard provisioner (Week 3 Phase 3).

The HTTP layer is faked in-memory; no live Langfuse needed.
"""

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "provision_dashboard",
    Path(__file__).parent.parent.parent
    / "observability"
    / "provision_dashboard.py",
)
provision_dashboard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(provision_dashboard)

ProvisionError = provision_dashboard.ProvisionError


class FakeDashboards(provision_dashboard.LangfuseDashboards):
    """In-memory unstable-API double keyed by (method, path)."""

    def __init__(self):
        super().__init__(
            host="http://x", public_key="pk", secret_key="sk"
        )
        self.calls = []
        self.widgets = [
            {"id": "w-old", "name": "MCP servers UP", "view": "observations"}
        ]
        self.dashboards = []
        self.placements = {}  # dashboard_id -> [placement, ...]

    def _call(self, method, path, payload=None):
        self.calls.append((method, path))
        if (method, path) == ("GET", "/dashboard-widgets?page=1&limit=100"):
            return {
                "data": self.widgets,
                "meta": {"page": 1, "limit": 100, "totalItems": len(self.widgets), "totalPages": 1},
            }
        if method == "PATCH" and path.startswith("/dashboard-widgets/"):
            wid = path.rsplit("/", 1)[-1]
            for w in self.widgets:
                if w["id"] == wid:
                    w.update(payload)
                    return dict(w)
            raise AssertionError(f"unknown widget {wid}")
        if (method, path) == ("POST", "/dashboard-widgets"):
            created = {"id": "w-new", **payload}
            self.widgets.append(created)
            return dict(created)
        if (method, path) == ("GET", "/dashboards?limit=100"):
            return {"data": self.dashboards}
        if (method, path) == ("POST", "/dashboards"):
            created = {"id": "d-1", **payload}
            self.dashboards.append(created)
            self.placements["d-1"] = []
            return dict(created)
        if method == "GET" and path.startswith("/dashboards/"):
            did = path.rsplit("/", 1)[-1]
            return {"id": did, "definition": {"widgets": list(self.placements.get(did, []))}}
        if method == "DELETE" and "/placements/" in path:
            parts = path.split("/")
            did, pid = parts[2], parts[4]
            self.placements[did] = [p for p in self.placements[did] if p["id"] != pid]
            return {"message": "Placement successfully deleted"}
        if method == "POST" and path.endswith("/placements"):
            did = path.split("/")[2]
            created = {"id": f"p-{len(self.placements[did])}", **payload}
            self.placements[did].append(created)
            return dict(created)
        raise AssertionError(f"unstubbed {method} {path}")


def _snapshot(tmp_path):
    import json

    dashboard = {
        "name": "Studymate Dashboard",
        "description": "d",
        "filters": [],
        "definition": {
            "widgets": [
                {"type": "widget", "id": "old-p", "widgetId": "w-old", "x": 0, "y": 32, "width": 6, "height": 6}
            ]
        },
    }
    (tmp_path / "studymate-dashboard.json").write_text(json.dumps(dashboard), encoding="utf-8")
    widget = {
        "id": "w-old",
        "createdAt": "t",
        "updatedAt": "t",
        "name": "MCP servers UP",
        "description": "up",
        "view": "observations",
        "dimensions": [],
        "metrics": [],
        "filters": [],
        "chartType": "VERTICAL_BAR",
        "chartConfig": {"type": "VERTICAL_BAR"},
    }
    (tmp_path / "widget-mcp-servers-up.json").write_text(json.dumps(widget), encoding="utf-8")
    return str(tmp_path)


def test_upsert_updates_widget_by_name(tmp_path):
    """Verify an existing widget is PATCHed, never duplicated."""
    client = FakeDashboards()
    wid = client.upsert_widget({"name": "MCP servers UP", "description": "new desc"})
    assert wid == "w-old"
    assert ("POST", "/dashboard-widgets") not in client.calls
    assert next(w for w in client.widgets if w["id"] == "w-old")["description"] == "new desc"


def test_upsert_creates_missing_widget():
    """Verify an unknown widget name is POSTed."""
    client = FakeDashboards()
    wid = client.upsert_widget({"name": "Brand new", "view": "observations"})
    assert wid == "w-new"
    assert ("POST", "/dashboard-widgets") in client.calls


def test_provision_end_to_end(tmp_path):
    """Verify dashboard create + placement rebuild with fresh widget ids."""
    snap = _snapshot(tmp_path)
    client = FakeDashboards()
    provision_dashboard.provision(snap, client)

    assert len(client.dashboards) == 1
    tiles = client.placements["d-1"]
    assert len(tiles) == 1
    assert tiles[0]["widgetId"] == "w-old"  # matched by name, fresh id
    assert (tiles[0]["x"], tiles[0]["y"], tiles[0]["width"], tiles[0]["height"]) == (0, 32, 6, 6)


def test_missing_keys_fail_loud():
    """Verify absent secrets fail with a clear reason, not a traceback."""
    with pytest.raises(ProvisionError, match="LANGFUSE_PUBLIC_KEY"):
        provision_dashboard.LangfuseDashboards(host="http://x", public_key="", secret_key="")


def test_dangling_tile_skipped_with_warning(tmp_path, capsys):
    """Verify placements referencing deleted widgets are skipped, rest provisioned."""
    import json

    dashboard = {
        "name": "D",
        "description": "",
        "filters": [],
        "definition": {
            "widgets": [
                {"type": "widget", "id": "p", "widgetId": "w-ghost", "x": 0, "y": 0, "width": 6, "height": 6}
            ]
        },
    }
    (tmp_path / "studymate-dashboard.json").write_text(json.dumps(dashboard), encoding="utf-8")
    loaded, placements, widgets = provision_dashboard.load_snapshot(str(tmp_path))
    assert placements == [] and widgets == {}
    assert "WARNING" in capsys.readouterr().err
