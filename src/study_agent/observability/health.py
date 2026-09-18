"""Periodic MCP health heartbeat (Prometheus gauges + Langfuse traces).

Prometheus is pull-only and Langfuse widgets read ClickHouse, so one side
cannot feed the other. Instead this module probes each MCP server on a timer
and fans the result out to BOTH: ``studymate_mcp_up`` gauges for Prometheus
and ``mcp-health:*`` heartbeat traces for the Langfuse status widget.

Everything here is best-effort: probes never raise (failures become
``ok=False`` results) and the scheduler runs on a daemon thread that cannot
block the REPL.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from study_agent.observability.metrics import observe_mcp_health
from study_agent.observability.tracing import build_run_event, new_run_id

logger = logging.getLogger(__name__)

# Server tiers match the Langfuse tool-span vocabulary (engine tool records),
# so heartbeat rows and traffic rows filter identically in widgets.
EXTERNAL_MCP_SERVER = "external-mcp"
LOCAL_MCP_SERVER = "local-mcp"

HEARTBEAT_THREAD_ID = "mcp-health"
HEARTBEAT_THREAD_NAME = "mcp-health"

DEFAULT_PROBE_TIMEOUT_S = 20.0


@dataclass
class ProbeResult:
    """Outcome of one MCP liveness probe (never raises by construction)."""

    server: str
    label: str
    ok: bool = False
    latency_s: float = 0.0
    detail: str = ""


async def _list_bridge_tools(
    command: str, args: List[str], env: Dict[str, str], timeout_s: float
) -> List[str]:
    """Opens one MCP session, lists remote tools, closes it."""
    import subprocess
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    server = StdioServerParameters(command=command, args=list(args), env=dict(env))
    async with stdio_client(
        server, errlog=subprocess.DEVNULL
    ) as (read_stream, write_stream), ClientSession(
        read_stream, write_stream
    ) as session:
        await session.initialize()
        result = await asyncio.wait_for(session.list_tools(), timeout=timeout_s)
        return [tool.name for tool in result.tools]


def probe_external_bridge(
    command: str = "uvx",
    args: Optional[List[str]] = None,
    env: Optional[Dict[str, str]] = None,
    timeout_s: float = DEFAULT_PROBE_TIMEOUT_S,
) -> ProbeResult:
    """Probes the external (stdio) MCP bridge by listing its tools."""
    import os

    start = time.perf_counter()
    merged_env = {
        **os.environ.copy(),
        "LOG_LEVEL": "ERROR",
        "PYTHONWARNINGS": "ignore",
        **(env or {}),
    }
    try:
        names = asyncio.run(
            _list_bridge_tools(
                command,
                args if args is not None else [],
                merged_env,
                timeout_s,
            )
        )
        return ProbeResult(
            server=EXTERNAL_MCP_SERVER,
            label="duckduckgo-bridge",
            ok=True,
            latency_s=round(time.perf_counter() - start, 3),
            detail=f"{len(names)} remote tools: {', '.join(names[:8])}",
        )
    except Exception as e:
        return ProbeResult(
            server=EXTERNAL_MCP_SERVER,
            label="duckduckgo-bridge",
            ok=False,
            latency_s=round(time.perf_counter() - start, 3),
            detail=f"{type(e).__name__}: {str(e)[:200]}",
        )


def probe_obsidian_vault(vault_path: str | Path) -> ProbeResult:
    """Probes the local Obsidian vault (dir exists + notes listable)."""
    start = time.perf_counter()
    try:
        from study_agent.tools.mcp.services import ObsidianVaultService

        service = ObsidianVaultService(vault_path)
        notes = service.list_notes(limit=1)
        return ProbeResult(
            server=LOCAL_MCP_SERVER,
            label="obsidian-vault",
            ok=True,
            latency_s=round(time.perf_counter() - start, 3),
            detail=f"vault ready at {service.root} ({notes[:120]})",
        )
    except Exception as e:
        return ProbeResult(
            server=LOCAL_MCP_SERVER,
            label="obsidian-vault",
            ok=False,
            latency_s=round(time.perf_counter() - start, 3),
            detail=f"{type(e).__name__}: {str(e)[:200]}",
        )


def build_heartbeat_event(
    results: List[ProbeResult], *, model: str = ""
) -> Dict[str, Any]:
    """Builds one JSONL/Langfuse heartbeat event from a tick's results.

    Tool observations are named ``mcp-health:<label>`` so the status widget
    filters them by name prefix; ``server`` reuses the traffic vocabulary.
    """
    live = [r for r in results if r.ok]
    down = [r for r in results if not r.ok]
    status = "success" if results and not down else "error"
    parts = [
        f"{r.label}: {'ok' if r.ok else 'DOWN'} in {r.latency_s:.2f}s"
        + ("" if r.ok else f" ({r.detail})")
        for r in results
    ]
    summary = (
        f"MCP health: {len(live)}/{len(results)} servers live"
        + (f" ({'; '.join(parts)})" if parts else " (no probes configured)")
    )
    return build_run_event(
        model=model,
        status=status,
        run_id=new_run_id(),
        thread_id=HEARTBEAT_THREAD_ID,
        thread_name=HEARTBEAT_THREAD_NAME,
        iterations=0,
        final_response=summary,
        messages=[],
        turns=[],
        tools=[
            {
                "name": f"mcp-health:{r.label}",
                "source": "mcp",
                "server": r.server,
                "duration_ms": round(r.latency_s * 1000.0, 2),
                "status": "ok" if r.ok else "error",
                "arguments": {"detail": r.detail},
            }
            for r in results
        ],
        duration_s=round(sum(r.latency_s for r in results), 3),
        error="" if not down else "; ".join(f"{r.label}: {r.detail}" for r in down),
    )


class HealthHeartbeat:
    """Runs zero-arg probes on an interval (daemon thread, idempotent)."""

    def __init__(
        self,
        probes: List[Callable[[], ProbeResult]],
        interval_s: float = 60.0,
        on_result: Optional[Callable[[List[ProbeResult]], None]] = None,
    ) -> None:
        self._probes = list(probes or [])
        self._interval_s = max(5.0, float(interval_s or 60.0))
        self._on_result = on_result
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def tick(self) -> List[ProbeResult]:
        """Runs every probe once, records gauges, fires the callback."""
        results: List[ProbeResult] = []
        for probe in self._probes:
            try:
                result = probe()
            except Exception as e:  # pragma: no cover - probes must not raise
                logger.debug(f"Health probe raised (contract breach): {e}")
                result = ProbeResult(server="unknown", label="unknown", detail=str(e)[:200])
            results.append(result)
            try:
                observe_mcp_health(result.server, result.ok, result.latency_s)
            except Exception as e:
                logger.debug(f"Health metric skipped: {e}")
        if self._on_result is not None:
            try:
                self._on_result(results)
            except Exception as e:
                logger.debug(f"Health callback skipped: {e}")
        return results

    def start(self) -> "HealthHeartbeat":
        """Starts the daemon loop (first tick immediate); safe to repeat."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="mcp-health-heartbeat", daemon=True
            )
            self._thread.start()
            return self

    def stop(self) -> None:
        """Stops the loop (the thread is daemonic; process exit is enough)."""
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # pragma: no cover - defensive
                logger.debug(f"Heartbeat tick failed (run unaffected): {e}")
            self._stop.wait(self._interval_s)


__all__ = [
    "EXTERNAL_MCP_SERVER",
    "LOCAL_MCP_SERVER",
    "HEARTBEAT_THREAD_ID",
    "HEARTBEAT_THREAD_NAME",
    "DEFAULT_PROBE_TIMEOUT_S",
    "ProbeResult",
    "probe_external_bridge",
    "probe_obsidian_vault",
    "build_heartbeat_event",
    "HealthHeartbeat",
]
