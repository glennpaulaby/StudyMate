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

TRACE_SCHEMA_VERSION = 2

DEFAULT_TRACE_MAX_CHARS = 4000

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


def truncate_text(text: Any, max_chars: int = DEFAULT_TRACE_MAX_CHARS) -> Any:
    """Truncates long strings with a marker; passes non-strings through."""
    if not isinstance(text, str) or max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + f"…[truncated {len(text) - max_chars} chars]"


def truncate_args(arguments: Any, max_chars: int = DEFAULT_TRACE_MAX_CHARS) -> Any:
    """Truncates tool-call arguments (dict values or raw strings) with markers."""
    if isinstance(arguments, dict):
        return {
            k: (truncate_text(v, max_chars) if isinstance(v, str) else v)
            for k, v in arguments.items()
        }
    return truncate_text(arguments, max_chars)


def truncate_messages(messages: Any, max_chars: int = DEFAULT_TRACE_MAX_CHARS) -> Any:
    """Truncates per-message content in a message list with markers.

    Bounds Langfuse span payloads: a long context would otherwise ship in
    full on every trace. Non-dict entries and non-string contents pass
    through untouched; never raises (returns the input on any surprise).
    """
    if not isinstance(messages, list):
        return messages
    try:
        truncated = []
        for message in messages:
            if not isinstance(message, dict):
                truncated.append(message)
                continue
            copy = dict(message)
            if isinstance(copy.get("content"), str):
                copy["content"] = truncate_text(copy["content"], max_chars)
            truncated.append(copy)
        return truncated
    except Exception:
        return messages


def build_run_event(
    *,
    model: str,
    status: str,
    run_id: Optional[str] = None,
    parent_run_id: str = "",
    thread_id: str = "",
    thread_name: str = "",
    iterations: int = 0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    reasoning_tokens: int = 0,
    final_response: str = "",
    messages: Optional[List[Dict[str, Any]]] = None,
    turns: Optional[List[Dict[str, Any]]] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
    permission_decisions: Optional[List[Dict[str, Any]]] = None,
    compactions: int = 0,
    duration_s: float = 0.0,
    error: str = "",
    model_parameters: Optional[Dict[str, Any]] = None,
    available_tools: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Builds one schema-v2 trace event (pure, no I/O — trivially testable).

    `messages` is the verbatim context snapshot (system prompt included);
    `turns` holds per-iteration LLM responses plus tool observations.
    Tool arguments are truncated with markers; everything else is verbatim.
    Readers must tolerate missing keys (v1 lines predate thread/messages).
    """
    return {
        "schema": TRACE_SCHEMA_VERSION,
        "run_id": run_id or new_run_id(),
        "parent_run_id": parent_run_id,
        "timestamp": _utc_now_iso(),
        "model": model,
        "model_parameters": model_parameters or {},
        "available_tools": available_tools or [],
        "thread": {"id": thread_id, "name": thread_name},
        "iterations": iterations,
        "tokens": {
            "prompt": prompt_tokens,
            "completion": completion_tokens,
            "reasoning": reasoning_tokens,
            "total": prompt_tokens + completion_tokens,
        },
        "final_response": final_response,
        "messages": messages or [],
        "turns": turns or [],
        "tools": tools or [],
        "permission_decisions": permission_decisions or [],
        "compactions": compactions,
        "duration_s": round(duration_s, 3),
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


def build_usage_details(
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
) -> Dict[str, int]:
    """Maps harness token counts onto Langfuse's usage_details envelope.

    Only input/output/total are sent: reasoning tokens are already counted
    inside completion/output per the OpenAI envelope, and a separate key
    risks double-billing cost. The reasoning breakout stays visible in
    ``metadata.tokens`` and per-turn records instead.
    """
    prompt = max(0, int(prompt_tokens or 0))
    completion = max(0, int(completion_tokens or 0))
    total = max(0, int(total_tokens or (prompt + completion)))
    return {"input": prompt, "output": completion, "total": total}


def _make_client(host: str, public_key: str, secret_key: str, timeout_s: float) -> Optional[Any]:
    """Builds a Langfuse client, or None when the SDK/keys are unavailable."""
    try:
        from langfuse import Langfuse
    except ImportError:
        logger.debug("Langfuse SDK missing; skipping export.")
        return None
    if not public_key or not secret_key:
        logger.debug("Langfuse keys unset; skipping export.")
        return None
    _quiet_otel_loggers()
    try:
        return Langfuse(
            public_key=public_key, secret_key=secret_key, host=host, timeout=timeout_s
        )
    except Exception as e:
        logger.warning(f"Langfuse client init failed (run unaffected): {e}")
        return None


def _enter_trace_context(
    thread_id: str, thread_name: str, run_id: str
) -> Optional[Any]:
    """Propagates session/tags/trace-name onto the current OTel context.

    Returns the context manager for later exit, or None when unavailable.
    Best-effort: trace-level columns (Session ID, Trace Tags, Trace Name)
    stay empty rather than ever failing a run.
    """
    try:
        from langfuse import propagate_attributes

        cm = propagate_attributes(
            session_id=thread_id or None,
            tags=[thread_name] if thread_name else None,
            trace_name=f"studymate:{thread_name}" if thread_name else f"studymate-run:{run_id[:8]}",
        )
        enter = getattr(cm, "__enter__", None)
        if callable(enter):
            enter()
            return cm
    except Exception as e:
        logger.debug(f"Langfuse trace context skipped: {e}")
    return None


def _exit_trace_context(cm: Optional[Any]) -> None:
    """Restores the OTel context entered by `_enter_trace_context`."""
    if cm is None:
        return
    try:
        exit_fn = getattr(cm, "__exit__", None)
        if callable(exit_fn):
            exit_fn(None, None, None)
    except Exception as e:
        logger.debug(f"Langfuse trace context exit skipped: {e}")


def _flush_async(client: Any) -> threading.Thread:
    """Flushes the OTel batch exporter off the hot path (daemon, never raises)."""

    def _target() -> None:
        try:
            client.flush()
        except Exception as e:
            logger.warning(f"Langfuse flush failed (run unaffected): {e}")

    thread = threading.Thread(target=_target, name="langfuse-flush", daemon=True)
    thread.start()
    return thread


def _run_tags(event: Dict[str, Any]) -> List[str]:
    """Trace tags for a finished run: thread name + outcome status.

    Tags (not the span name) are the queryable success/fail signal: the
    Traces table's Trace Tags column shows e.g. `success` / `error` /
    `max_iterations` / `empty` next to the thread name.
    """
    tags: List[str] = []
    thread = event.get("thread", {})
    name = thread.get("name") if isinstance(thread, dict) else ""
    if isinstance(name, str) and name.strip():
        tags.append(name.strip())
    status = str(event.get("status", "") or "").strip()
    if status:
        tags.append(status)
    return tags


def _set_trace_tags(span: Optional[Any], tags: List[str]) -> None:
    """Stamps trace tags onto a live span's OTel attributes. Never raises.

    Mirrors what `propagate_attributes(tags=...)` does at span creation
    (a `langfuse.trace.tags` list attribute, which the backend derives the
    Trace Tags column from) — but callable at finish time, when the run
    outcome is actually known.
    """
    try:
        clean = [str(t).strip() for t in (tags or [])]
        clean = [t for t in clean if t]
        if not clean:
            return
        otel_span = getattr(span, "_otel_span", None)
        setter = getattr(otel_span, "set_attribute", None)
        if not callable(setter):
            return
        try:
            from langfuse import LangfuseOtelSpanAttributes

            key = str(LangfuseOtelSpanAttributes.TRACE_TAGS)
        except ImportError:
            key = "langfuse.trace.tags"
        setter(key, clean)
    except Exception as e:
        logger.debug(f"Langfuse trace tags skipped: {e}")


def _root_output(
    event: Dict[str, Any],
    duration_s: float,
    max_chars: int = DEFAULT_TRACE_MAX_CHARS,
) -> Dict[str, Any]:
    """Shared agent-span output payload (turns, end-of-run messages, duration).

    `messages` is the end-of-run context snapshot (system prompt first), so
    the UI shows both per-turn deltas (generation children) and the final
    state (root). Per-message content is truncated to bound span payloads.
    `status` is explicit here (and in metadata) so the outcome never lives
    only in the span name.
    """
    return {
        "final_response": event.get("final_response", ""),
        "turns": event.get("turns", []),
        "tools": event.get("tools", []),
        "messages": truncate_messages(event.get("messages", []), max_chars),
        "duration_s": duration_s,
        "status": str(event.get("status", "unknown")),
        "error": event.get("error", ""),
    }


def _root_metadata(event: Dict[str, Any], duration_s: float) -> Dict[str, Any]:
    """Shared agent-span metadata payload (model, tokens, thread, tools)."""
    tokens = event.get("tokens", {}) if isinstance(event.get("tokens"), dict) else {}
    return {
        "model": event.get("model", ""),
        "model_parameters": event.get("model_parameters", {}),
        "available_tools": event.get("available_tools", []),
        "parent_run_id": event.get("parent_run_id", ""),
        "compactions": event.get("compactions", 0),
        "tokens": tokens,
        "duration_s": duration_s,
        "status": str(event.get("status", "unknown")),
    }


def _emit_child_observations(
    parent: Any,
    event: Dict[str, Any],
    *,
    max_chars: int = DEFAULT_TRACE_MAX_CHARS,
) -> None:
    """Adds per-turn generation + per-tool children to a live or post-hoc span.

    Generation-type children are what populate Langfuse's Provided Model
    Name, Usage, Cost (once prices are configured), Time To First Token and
    Tool Calls columns — attributes the SDK drops on span-like observations,
    which is why the parent stays ``as_type="agent"``. Each child is
    individually guarded so one oversized payload never fails the export.
    """
    model = str(event.get("model", ""))
    params = event.get("model_parameters", {})
    model_parameters = params if isinstance(params, dict) else {}
    available = event.get("available_tools", [])
    available_tools = available if isinstance(available, list) else []
    turns = event.get("turns", [])
    if not isinstance(turns, list):
        return
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        try:
            llm = turn.get("llm", {}) if isinstance(turn.get("llm"), dict) else {}
            tools = turn.get("tools", []) if isinstance(turn.get("tools"), list) else []
            gen = parent.start_observation(
                name=f"llm-turn-{turn.get('n', '?')}",
                as_type="generation",
                input={"turn": turn.get("n"), "available_tools": available_tools},
                output={
                    "content": truncate_text(llm.get("content", ""), max_chars),
                    "reasoning_content": truncate_text(
                        llm.get("reasoning_content", "") or "", max_chars
                    ),
                    "tool_calls": llm.get("tool_calls", []),
                    "finish_reason": llm.get("finish_reason", ""),
                },
                metadata={"turn": turn.get("n")},
                model=model or None,
                model_parameters=model_parameters or None,
                usage_details=build_usage_details(
                    llm.get("prompt_tokens", 0),
                    llm.get("completion_tokens", 0),
                    (llm.get("prompt_tokens", 0) or 0) + (llm.get("completion_tokens", 0) or 0),
                ),
                # No completion_start_time post-hoc: backdating it would fake
                # Time To First Token. Live generations set it for real.
            )
        except Exception as e:
            logger.debug(f"Langfuse generation child skipped: {e}")
            continue
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            try:
                args: Any = ""
                calls = llm.get("tool_calls", [])
                if isinstance(calls, list):
                    for call in calls:
                        if isinstance(call, dict) and call.get("name") == tool.get("name"):
                            args = call.get("arguments", "")
                            break
                child = gen.start_observation(
                    name=str(tool.get("name", "tool")),
                    as_type="tool",
                    input=truncate_args(args, max_chars),
                    output=truncate_text(str(tool.get("observation", "")), max_chars),
                    metadata={"turn": turn.get("n")},
                )
                child.end()
            except Exception as e:
                logger.debug(f"Langfuse tool child skipped: {e}")
                continue
        try:
            gen.end()
        except Exception as e:
            logger.debug(f"Langfuse generation end skipped: {e}")


def start_langfuse_run(
    *,
    host: str,
    public_key: str = "",
    secret_key: str = "",
    timeout_s: float = 30.0,
    run_id: str = "",
    model: str = "",
    thread_id: str = "",
    thread_name: str = "",
    user_message: str = "",
    model_parameters: Optional[Dict[str, Any]] = None,
    available_tools: Optional[List[str]] = None,
    system_prompt: str = "",
    tool_schemas: Optional[List[Dict[str, Any]]] = None,
    tool_choice: str = "auto",
    injected_memory: str = "",
    max_chars: int = DEFAULT_TRACE_MAX_CHARS,
) -> Optional[Dict[str, Any]]:
    """Opens a live root agent span at run start (true Latency). Never raises.

    The span stays open for the whole ReAct loop, so the UI's Latency column
    measures the real run. Root input captures the run's full starting
    request (system prompt, injected memory block, tool schemas);
    per-turn deltas ride on the generation children, the end-of-run snapshot
    on the finish output. Returns an opaque handle for the per-turn helpers
    and `finish_langfuse_run`, or None when exporting is unavailable.
    """
    client = _make_client(host, public_key, secret_key, timeout_s)
    if client is None:
        return None
    if not _host_reachable(host):
        logger.warning(f"Langfuse trace export skipped (host unreachable, run unaffected): {host}")
        return None
    try:
        from langfuse.types import TraceContext
    except ImportError:
        logger.debug("Langfuse SDK types missing; skipping export.")
        return None
    prop_ctx = _enter_trace_context(thread_id, thread_name, run_id or "unknown")
    try:
        thread = {"id": thread_id, "name": thread_name}
        span = client.start_observation(
            trace_context=TraceContext(
                trace_id=_langfuse_trace_id(client, run_id)
            ),
            name=f"studymate-run:{thread_name or (run_id[:8] if run_id else 'run')}",
            as_type="agent",
            input={
                "thread": thread,
                "user_message": truncate_text(user_message, max_chars),
                "system_prompt": truncate_text(system_prompt, max_chars),
                # Memory/procedure note as its own field: it rides in the
                # system prompt tail, which truncation would otherwise amputate.
                "injected_memory": truncate_text(injected_memory, max_chars),
                "tool_choice": tool_choice,
                "tool_schemas": tool_schemas or [],
            },
            metadata={
                "model": model,
                "model_parameters": model_parameters or {},
                "available_tools": available_tools or [],
                "thread": thread,
            },
            level="DEFAULT",
        )
        return {
            "client": client,
            "span": span,
            "prop_ctx": prop_ctx,
            "run_id": run_id,
            "max_chars": max_chars,
        }
    except Exception as e:
        logger.warning(f"Langfuse trace start failed (run unaffected): {e}")
        _exit_trace_context(prop_ctx)
        return None


def start_langfuse_generation(
    handle: Optional[Dict[str, Any]],
    *,
    name: str,
    turn: Optional[int] = None,
    available_tools: Optional[List[str]] = None,
    new_messages: Optional[List[Dict[str, Any]]] = None,
    compacted: bool = False,
) -> Optional[Any]:
    """Opens a live generation child before an LLM call. Never raises.

    `new_messages` carries only the context entries added since the previous
    call (each call's input = previous input + delta), so the children chain
    reconstructs the full request with zero duplication. When compaction
    replaced history mid-run, the caller passes the full snapshot once with
    `compacted=True` instead of a delta.
    """
    if not handle or "span" not in handle:
        return None
    try:
        max_chars = handle.get("max_chars", DEFAULT_TRACE_MAX_CHARS)
        try:
            max_chars = int(max_chars)
        except (TypeError, ValueError):
            max_chars = DEFAULT_TRACE_MAX_CHARS
        return handle["span"].start_observation(
            name=name,
            as_type="generation",
            input={
                "turn": turn,
                "available_tools": available_tools or [],
                "new_messages": truncate_messages(new_messages or [], max_chars),
                "compacted": compacted,
            },
            metadata={"turn": turn} if turn is not None else {},
        )
    except Exception as e:
        logger.debug(f"Langfuse generation start skipped: {e}")
        return None


def finish_langfuse_generation(
    span: Optional[Any],
    *,
    output: Optional[Dict[str, Any]] = None,
    model: str = "",
    usage_details: Optional[Dict[str, int]] = None,
    model_parameters: Optional[Dict[str, Any]] = None,
    completion_start_time: Optional[Any] = None,
    level: str = "DEFAULT",
    status_message: Optional[str] = None,
) -> None:
    """Fills a live generation child (model/usage/TTFT) and ends it."""
    if span is None:
        return
    try:
        span.update(
            output=output,
            model=model or None,
            usage_details=usage_details,
            model_parameters=model_parameters or None,
            completion_start_time=completion_start_time,
            level=level,  # type: ignore[arg-type]
            status_message=status_message,
        )
    except Exception as e:
        logger.debug(f"Langfuse generation update skipped: {e}")
    try:
        span.end()
    except Exception as e:
        logger.debug(f"Langfuse generation end skipped: {e}")


def start_langfuse_tool(
    parent: Optional[Any],
    *,
    name: str,
    input: Optional[Any] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[Any]:
    """Opens a live tool child before a tool executes. Never raises."""
    if parent is None:
        return None
    try:
        return parent.start_observation(
            name=name,
            as_type="tool",
            input=input,
            metadata=metadata or {},
        )
    except Exception as e:
        logger.debug(f"Langfuse tool start skipped: {e}")
        return None


def finish_langfuse_tool(
    span: Optional[Any],
    *,
    output: Optional[Any] = None,
    metadata: Optional[Dict[str, Any]] = None,
    level: str = "DEFAULT",
    status_message: Optional[str] = None,
) -> None:
    """Fills a live tool child and ends it (true tool latency)."""
    if span is None:
        return
    try:
        span.update(
            output=output,
            metadata=metadata or {},
            level=level,  # type: ignore[arg-type]
            status_message=status_message,
        )
    except Exception as e:
        logger.debug(f"Langfuse tool update skipped: {e}")
    try:
        span.end()
    except Exception as e:
        logger.debug(f"Langfuse tool end skipped: {e}")


def finish_langfuse_run(handle: Optional[Dict[str, Any]], event: Dict[str, Any]) -> bool:
    """Closes the live root span with the final event (true Latency).

    `update` + `end` are synchronous and cheap (just stamp times); only the
    network `flush` goes to a daemon thread. Never raises.
    """
    if not handle or "span" not in handle or "client" not in handle:
        return False
    span, client = handle["span"], handle["client"]
    try:
        status = str(event.get("status", "unknown"))
        duration_s = float(event.get("duration_s", 0.0) or 0.0)
        try:
            max_chars = int(handle.get("max_chars", DEFAULT_TRACE_MAX_CHARS))
        except (TypeError, ValueError):
            max_chars = DEFAULT_TRACE_MAX_CHARS
        span.update(
            name=f"studymate-run:{status} ({duration_s:.1f}s)",
            output=_root_output(event, duration_s, max_chars),
            metadata=_root_metadata(event, duration_s),
            level="ERROR" if status == "error" else "DEFAULT",  # type: ignore[arg-type]
            status_message=str(event.get("error", "")) or None,
        )
    except Exception as e:
        logger.debug(f"Langfuse run update skipped: {e}")
    _set_trace_tags(span, _run_tags(event))
    try:
        span.end()
    except Exception as e:
        logger.debug(f"Langfuse run end skipped: {e}")
    finally:
        _exit_trace_context(handle.get("prop_ctx"))
    _flush_async(client)
    return True


def abort_langfuse_run(handle: Optional[Dict[str, Any]], error: str) -> bool:
    """Closes a live span when the run raises (LLM failure path). Never raises."""
    if not handle:
        return False
    return finish_langfuse_run(
        handle,
        {"status": "error", "duration_s": 0.0, "final_response": "", "error": error},
    )


def export_langfuse(
    event: Dict[str, Any],
    *,
    host: str,
    public_key: str = "",
    secret_key: str = "",
    timeout_s: float = 30.0,
) -> bool:
    """Sends one event to self-hosted Langfuse (v4 OTel API). Never raises.

    Post-hoc fallback used when no live span exists (error paths outside the
    engine, direct callers). The span duration is ~0 by construction — the
    true-latency path is `start_langfuse_run`/`finish_langfuse_run`, which the
    engine holds open across the whole ReAct loop.
    """
    client = _make_client(host, public_key, secret_key, timeout_s)
    if client is None:
        return False
    if not _host_reachable(host):
        logger.warning(f"Langfuse trace export skipped (host unreachable, run unaffected): {host}")
        return False
    try:
        from langfuse.types import TraceContext

        status = str(event.get("status", "unknown"))
        thread = event.get("thread", {}) if isinstance(event.get("thread"), dict) else {}
        messages = event.get("messages", [])
        duration_s = float(event.get("duration_s", 0.0) or 0.0)
        span = client.start_observation(
            trace_context=TraceContext(
                trace_id=_langfuse_trace_id(client, str(event.get("run_id", "")))
            ),
            name=f"studymate-run:{status} ({duration_s:.1f}s)",
            as_type="agent",
            input={
                "thread": thread,
                "message_count": len(messages) if isinstance(messages, list) else 0,
                "messages": messages,
            },
            output=_root_output(event, duration_s),
            metadata=_root_metadata(event, duration_s),
            level="ERROR" if status == "error" else "DEFAULT",
            status_message=str(event.get("error", "")) or None,
        )
        _emit_child_observations(span, event)
        _set_trace_tags(span, _run_tags(event))
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
