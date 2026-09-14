"""Harness-owned run traces (Week 3 Task 1).

Traces live under ``./observability/`` — outside the agent's write jail
(``workspace/artifacts``) — and are written with plain file I/O, never via
``ToolRegistry``, so the LLM cannot read or forge them.

Each agent run appends exactly one JSON line (crash-safe single ``O_APPEND``
write). Langfuse export is best-effort and backgrounded: a down exporter
never fails a run.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

TRACE_SCHEMA_VERSION = 1

_OTEL_LOGGERS_QUIETED = False


def _quiet_otel_loggers() -> None:
    """Silences the OTel SDK's stderr spam (BatchSpanProcessor error logs).

    Those logs fire from a background exporter thread and land mid-prompt in
    the REPL. Our own one-line warning below carries the same signal, so
    nothing is lost — it just renders sanely via prompt_toolkit.
    """
    global _OTEL_LOGGERS_QUIETED
    if _OTEL_LOGGERS_QUIETED:
        return
    for name in (
        "opentelemetry",
        "opentelemetry.exporter.otlp",
        "opentelemetry.sdk.trace.export",
    ):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    _OTEL_LOGGERS_QUIETED = True


def new_run_id() -> str:
    """Generates a unique run identifier (also used as Langfuse trace id)."""
    return uuid.uuid4().hex


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_run_event(
    *,
    model: str,
    status: str,
    run_id: Optional[str] = None,
    parent_run_id: str = "",
    thread: str = "",
    iterations: int = 0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    tools: Optional[List[Dict[str, Any]]] = None,
    permission_decisions: Optional[List[Dict[str, Any]]] = None,
    compactions: int = 0,
    error: str = "",
) -> Dict[str, Any]:
    """Builds one schema-v1 trace event (pure, no I/O — trivially testable)."""
    return {
        "schema": TRACE_SCHEMA_VERSION,
        "run_id": run_id or new_run_id(),
        "parent_run_id": parent_run_id,
        "timestamp": _utc_now_iso(),
        "model": model,
        "thread": thread,
        "iterations": iterations,
        "tokens": {
            "prompt": prompt_tokens,
            "completion": completion_tokens,
            "total": prompt_tokens + completion_tokens,
        },
        "tools": tools or [],
        "permission_decisions": permission_decisions or [],
        "compactions": compactions,
        "status": status,
        "error": error,
    }


def append_trace(trace_file: str | Path, event: Dict[str, Any]) -> Optional[Path]:
    """Appends one event as a single JSON line. Returns path, None on failure."""
    path = Path(trace_file)
    try:
        if path.parent and str(path.parent) not in ("", "."):
            path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, ensure_ascii=False) + "\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
        return path
    except Exception as e:
        logger.warning(f"Trace append failed ({path}): {e}")
        return None


def _langfuse_trace_id(client: Any, run_id: str) -> str:
    """Keeps our 32-hex run id (the backend-validated format); else mints one."""
    cleaned = (run_id or "").strip().lower().replace("-", "")
    if len(cleaned) == 32 and all(c in "0123456789abcdef" for c in cleaned):
        return cleaned
    try:
        return str(client.create_trace_id())
    except Exception:
        return uuid.uuid4().hex


def _host_reachable(host: str, timeout_s: float = 3.0) -> bool:
    """Fast pre-check so a down server fails loudly instead of timing out.

    Any HTTP response counts as alive (even 404 — the health path varies by
    server version); only connection-level failures return False.
    """
    from urllib import error as urlerror
    from urllib import request as urlrequest

    try:
        with urlrequest.urlopen(host.rstrip("/") + "/api/public/health", timeout=timeout_s):
            return True
    except urlerror.HTTPError:
        return True
    except Exception:
        return False
    """Keeps our 32-hex run id (the backend-validated format); else mints one."""
    cleaned = (run_id or "").strip().lower().replace("-", "")
    if len(cleaned) == 32 and all(c in "0123456789abcdef" for c in cleaned):
        return cleaned
    try:
        return str(client.create_trace_id())
    except Exception:
        return uuid.uuid4().hex


def export_langfuse(
    event: Dict[str, Any],
    *,
    host: str,
    public_key: str = "",
    secret_key: str = "",
    timeout_s: float = 30.0,
) -> bool:
    """Sends one event to self-hosted Langfuse (v4 OTel API). Never raises."""
    try:
        from langfuse import Langfuse
        from langfuse.types import TraceContext
    except ImportError:
        logger.debug("Langfuse SDK missing; skipping export.")
        return False
    if not public_key or not secret_key:
        logger.debug("Langfuse keys unset; skipping export.")
        return False
    _quiet_otel_loggers()
    if not _host_reachable(host):
        logger.warning(f"Langfuse trace export skipped (host unreachable, run unaffected): {host}")
        return False
    try:
        client = Langfuse(
            public_key=public_key, secret_key=secret_key, host=host, timeout=timeout_s
        )
        tokens = event.get("tokens", {}) if isinstance(event.get("tokens"), dict) else {}
        status = str(event.get("status", "unknown"))
        span = client.start_observation(
            trace_context=TraceContext(
                trace_id=_langfuse_trace_id(client, str(event.get("run_id", "")))
            ),
            name=f"studymate-run:{status}",
            as_type="agent",
            input={
                "thread": event.get("thread", ""),
                "iterations": event.get("iterations", 0),
                "prompt_tokens": tokens.get("prompt", 0),
            },
            output={
                "completion_tokens": tokens.get("completion", 0),
                "tools": event.get("tools", []),
                "error": event.get("error", ""),
            },
            metadata={
                "model": event.get("model", ""),
                "parent_run_id": event.get("parent_run_id", ""),
                "compactions": event.get("compactions", 0),
            },
            level="ERROR" if status == "error" else "DEFAULT",
            status_message=str(event.get("error", "")) or None,
        )
        span.end()
        client.flush()
        return True
    except Exception as e:
        logger.warning(f"Langfuse trace export failed (run unaffected): {e}")
        return False


def export_langfuse_async(event: Dict[str, Any], **kwargs: Any) -> threading.Thread:
    """Fire-and-forget Langfuse export on a daemon thread (never blocks a run)."""

    def _target() -> None:
        export_langfuse(event, **kwargs)

    thread = threading.Thread(target=_target, name="langfuse-export", daemon=True)
    thread.start()
    return thread
