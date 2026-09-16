#!/usr/bin/env python3
"""Idempotent Langfuse dashboard provisioner (Week 3 Phase 3).

Reads the committed snapshots in ``observability/langfuse-dashboard/``
and upserts them into the project behind the ``LANGFUSE_*`` env keys via
the unstable public API (``/api/public/unstable``):

- widgets are matched **by name** (PATCH update, else POST create),
- the dashboard is matched **by name** (reuse, else POST create),
- placements are rebuilt from the snapshot (delete-all, then re-add with
  explicit x/y/width/height pointing at the fresh widget ids).

Stdlib only (urllib) so it runs in ``python:3.12-slim`` with no install.
Exit 0 on success; non-zero with a one-line reason on failure. Runs as a
one-shot compose service (``restart: "no"``) so a failure never blocks
stack boot — re-run with ``docker compose run --rm provision-dashboard``.
"""

from __future__ import annotations

import base64
import glob
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

SNAPSHOT_GLOB = "widget-*.json"
DASHBOARD_SNAPSHOT = "studymate-dashboard.json"

# Fields accepted by POST/PATCH /dashboard-widgets (server-generated
# id/createdAt/updatedAt must NOT be sent).
WIDGET_SPEC_FIELDS = (
    "name",
    "description",
    "view",
    "dimensions",
    "metrics",
    "filters",
    "chartType",
    "chartConfig",
)


class ProvisionError(RuntimeError):
    """Fatal provisioning failure with a human-readable reason."""


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip().strip("\"'")


class LangfuseDashboards:
    """Thin client for the unstable dashboards/placements/widgets API."""

    def __init__(self, host: str, public_key: str, secret_key: str) -> None:
        if not public_key or not secret_key:
            raise ProvisionError(
                "missing LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY "
                "(sign up at the Langfuse UI, create a project, copy its keys)"
            )
        token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
        self._base = host.rstrip("/")
        self._headers = {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
        }

    def _call(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self._base}/api/public/unstable{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, headers=self._headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode() or "{}"
                return json.loads(body)
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:300]
            raise ProvisionError(f"{method} {path} -> HTTP {e.code}: {detail}")
        except OSError as e:
            raise ProvisionError(f"{method} {path} -> unreachable ({e})")

    # -- widgets ------------------------------------------------------
    def list_widgets(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        page = 1
        while True:
            res = self._call("GET", f"/dashboard-widgets?page={page}&limit=100")
            out.extend(res.get("data", []))
            meta = res.get("meta", {})
            if page >= int(meta.get("totalPages", 1)):
                return out
            page += 1

    def upsert_widget(self, spec: Dict[str, Any]) -> str:
        """Creates or updates a widget by name; returns its (fresh) id."""
        payload = {k: spec[k] for k in WIDGET_SPEC_FIELDS if k in spec}
        for existing in self.list_widgets():
            if existing.get("name") == spec.get("name"):
                updated = self._call(
                    "PATCH", f"/dashboard-widgets/{existing['id']}", payload
                )
                print(f"  updated widget: {spec.get('name')}")
                return str(updated.get("id", existing["id"]))
        created = self._call("POST", "/dashboard-widgets", payload)
        print(f"  created widget: {spec.get('name')}")
        return str(created["id"])

    # -- dashboards ---------------------------------------------------
    def list_dashboards(self) -> List[Dict[str, Any]]:
        res = self._call("GET", "/dashboards?limit=100")
        return list(res.get("data", []))

    def ensure_dashboard(
        self, name: str, description: str, filters: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        for existing in self.list_dashboards():
            if existing.get("name") == name:
                updated = self._call(
                    "PATCH",
                    f"/dashboards/{existing['id']}",
                    {"description": description, "filters": filters},
                )
                print(f"reusing dashboard: {name} ({existing['id']})")
                return updated
        created = self._call(
            "POST",
            "/dashboards",
            {"name": name, "description": description, "filters": filters},
        )
        print(f"created dashboard: {name} ({created['id']})")
        return created


def load_snapshot(snapshot_dir: str) -> tuple:
    """Returns (dashboard_spec, placement_specs, widget_specs_by_name)."""
    with open(os.path.join(snapshot_dir, DASHBOARD_SNAPSHOT), encoding="utf-8") as f:
        dashboard = json.load(f)
    widgets: Dict[str, Dict[str, Any]] = {}
    for path in sorted(glob.glob(os.path.join(snapshot_dir, SNAPSHOT_GLOB))):
        with open(path, encoding="utf-8") as f:
            spec = json.load(f)
        widgets[str(spec["name"])] = spec
    # Map snapshot widget ids -> names so placements resolve to fresh ids.
    id_to_name = {str(w.get("id", "")): name for name, w in widgets.items()}
    placements = dashboard.get("definition", {}).get("widgets", [])
    for placement in placements:
        old_id = str(placement.get("widgetId", ""))
        if placement.get("type") == "widget" and old_id and old_id not in id_to_name:
            print(
                f"  WARNING: skipping tile at "
                f"(x={placement.get('x')}, y={placement.get('y')}) "
                f"referencing deleted widget id {old_id}",
                file=sys.stderr,
            )
            placement["_skip"] = True
            continue
        placement["_widget_name"] = id_to_name.get(old_id, "")
    placements = [p for p in placements if not p.get("_skip")]
    return dashboard, placements, widgets


def provision(snapshot_dir: str, client: LangfuseDashboards) -> None:
    dashboard, placements, widgets = load_snapshot(snapshot_dir)
    fresh_ids: Dict[str, str] = {}
    for name, spec in widgets.items():
        fresh_ids[name] = client.upsert_widget(spec)
    live = client.ensure_dashboard(
        str(dashboard.get("name", "Studymate Dashboard")),
        str(dashboard.get("description", "")),
        list(dashboard.get("filters", [])),
    )
    # Resolve placements through names (snapshot ids are instance-local).
    resolved = 0
    current = client._call("GET", f"/dashboards/{live['id']}")
    for placement in current.get("definition", {}).get("widgets", []):
        client._call("DELETE", f"/dashboards/{live['id']}/placements/{placement['id']}")
    for placement in placements:
        if placement.get("type") != "widget":
            continue
        name = placement.get("_widget_name", "")
        if name not in fresh_ids:
            raise ProvisionError(f"no fresh id for widget {name!r}")
        client._call(
            "POST",
            f"/dashboards/{live['id']}/placements",
            {
                "type": "widget",
                "widgetId": fresh_ids[name],
                "x": placement.get("x", 0),
                "y": placement.get("y", 0),
                "width": placement.get("width", 6),
                "height": placement.get("height", 6),
            },
        )
        resolved += 1
    print(f"placed {resolved} tiles on '{live.get('name')}'")


def main(argv: List[str]) -> int:
    snapshot_dir = argv[1] if len(argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "langfuse-dashboard"
    )
    # Boot race: langfuse-web cold-boots in minutes; retry unreachable hosts.
    try:
        wait_retries = int(os.getenv("PROVISION_WAIT_RETRIES", "24"))
    except ValueError:
        wait_retries = 24
    attempt = 0
    while True:
        try:
            client = LangfuseDashboards(
                host=_env("LANGFUSE_HOST", "http://localhost:3000"),
                public_key=_env("LANGFUSE_PUBLIC_KEY"),
                secret_key=_env("LANGFUSE_SECRET_KEY"),
            )
            # Liveness probe before mutating anything.
            client._call("GET", "/dashboard-widgets?page=1&limit=1")
            provision(snapshot_dir, client)
            break
        except ProvisionError as e:
            attempt += 1
            if "unreachable" in str(e) and attempt <= wait_retries:
                time.sleep(10)
                continue
            print(f"provision-dashboard: FAILED: {e}", file=sys.stderr)
            return 1
    print("provision-dashboard: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
