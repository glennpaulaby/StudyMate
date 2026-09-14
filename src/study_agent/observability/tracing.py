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


def export_langfuse(
    event: Dict[str, Any],
    *,
    host: str,
    public_key: str = "",
    secret_key: str = "",
    timeout_s: float = 10.0,
) -> bool:
    """Sends one event to self-hosted Langfuse. Never raises; False when skipped."""
    try:
        from langfuse import Langfuse
    except ImportError:
        logger.debug("Langfuse SDK missing; skipping export.")
        return False
    if not public_key or not secret_key:
        logger.debug("Langfuse keys unset; skipping export.")
        return False
    try:
        client = Langfuse(public_key=public_key, secret_key=secret_key, host=host)
        tokens = event.get("tokens", {}) if isinstance(event.get("tokens"), dict) else {}
        client.trace(
            id=event.get("run_id"),
            name=f"studymate-run:{event.get('status', 'unknown')}",
            metadata={
                "model": event.get("model", ""),
                "thread": event.get("thread", ""),
                "iterations": event.get("iterations", 0),
                "parent_run_id": event.get("parent_run_id", ""),
                "compactions": event.get("compactions", 0),
            },
            input={"prompt_tokens": tokens.get("prompt", 0)},
            output={
                "completion_tokens": tokens.get("completion", 0),
                "tools": event.get("tools", []),
                "error": event.get("error", ""),
            },
        )
        client.flush()
        _ = timeout_s
        return True
    except Exception as e:
        logger.warning(f"Langfuse export failed: {e}")
        return False


def export_langfuse_async(event: Dict[str, Any], **kwargs: Any) -> threading.Thread:
    """Fire-and-forget Langfuse export on a daemon thread (never blocks a run)."""

    def _target() -> None:
        export_langfuse(event, **kwargs)

    thread = threading.Thread(target=_target, name="langfuse-export", daemon=True)
    thread.start()
    return thread
