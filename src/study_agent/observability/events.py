"""Standalone trace events for cross-cutting monitors (Week 3).

Heartbeat-style events — one JSONL line + one Langfuse trace each — for
concerns that live outside any single agent run:

- subagent delegations (``subagent:<name>`` tool records, this module),
- permission decisions (``permission:<tool>`` records, Phase 2).

Widgets filter by name prefix, mirroring the ``mcp-health:*`` convention
in ``health.py``. Builders are pure (no I/O); callers fan out via
``append_trace`` + ``export_langfuse_async``, best-effort.
"""

from typing import Any, Dict, List

from study_agent.observability.tracing import build_run_event, new_run_id
from study_agent.security.policy import PermissionEvent

SUBAGENT_THREAD_ID = "subagent-runs"
SUBAGENT_THREAD_NAME = "Subagent delegations"
PERMISSION_THREAD_ID = "permission-decisions"
PERMISSION_THREAD_NAME = "Permission decisions"


def build_subagent_event(
    *,
    subagent_name: str,
    status: str,
    parent_run_id: str = "",
    child_run_id: str = "",
    model: str = "",
    iterations: int = 0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    reasoning_tokens: int = 0,
    duration_s: float = 0.0,
    summary: str = "",
    error: str = "",
) -> Dict[str, Any]:
    """Builds one JSONL/Langfuse subagent delegation event.

    ``status`` is ``"success"`` or ``"error"`` (timeouts surface as errors
    with the detail in ``error``). The delegation is a ``subagent:<name>``
    tool record so widgets filter it by name prefix; ``parent_run_id``
    links it to the delegating run's trace.
    """
    ok = status == "success"
    detail = summary if ok else (error or f"subagent '{subagent_name}' failed")
    return build_run_event(
        model=model,
        status="success" if ok else "error",
        run_id=child_run_id or new_run_id(),
        parent_run_id=parent_run_id,
        thread_id=SUBAGENT_THREAD_ID,
        thread_name=SUBAGENT_THREAD_NAME,
        iterations=iterations,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        reasoning_tokens=reasoning_tokens,
        final_response=detail,
        messages=[],
        turns=[],
        tools=[
            {
                "name": f"subagent:{subagent_name}",
                "source": "subagent",
                "server": "",
                "duration_ms": round(duration_s * 1000.0, 2),
                "status": "ok" if ok else "error",
                "arguments": {"parent_run_id": parent_run_id},
            }
        ],
        duration_s=duration_s,
        error="" if ok else detail,
    )


__all__ = [
    "SUBAGENT_THREAD_ID",
    "SUBAGENT_THREAD_NAME",
    "PERMISSION_THREAD_ID",
    "PERMISSION_THREAD_NAME",
    "build_subagent_event",
    "build_permission_event",
]


def build_permission_event(
    decisions: List[PermissionEvent], *, model: str = ""
) -> Dict[str, Any]:
    """Builds one batched permission-decision event for a turn.

    Each gated tool call becomes a ``permission:<tool>`` record carrying
    the decision, whether it executed, and the matched rule — widgets
    filter by name prefix and slice by ``decision``. Recording always
    succeeds as an event (``status="success"``); refusals surface per
    record (``executed=false`` → ``status="error"``).
    """
    records = [
        {
            "name": f"permission:{d.tool}",
            "source": "permission",
            "server": "",
            "duration_ms": 0.0,
            "status": "ok" if d.executed else "error",
            "arguments": {
                "decision": d.decision.value,
                "executed": d.executed,
                "rule": d.source,
            },
        }
        for d in decisions
    ]
    executed = sum(1 for d in decisions if d.executed)
    summary = f"Permissions: {executed}/{len(decisions)} gated calls executed"
    return build_run_event(
        model=model,
        status="success",
        thread_id=PERMISSION_THREAD_ID,
        thread_name=PERMISSION_THREAD_NAME,
        final_response=summary,
        messages=[],
        turns=[],
        tools=records,
    )
