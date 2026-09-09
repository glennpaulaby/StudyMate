import json as _json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from study_agent.config.settings import CompactionConfig
from study_agent.core.types import ChatMessage, Role
from study_agent.llm.base import BaseLLMClient

logger = logging.getLogger(__name__)

CHARS_PER_TOKEN = 4  # OpenCode-parity heuristic: serialized request, 4 chars/token
PRUNED_TOOL_PLACEHOLDER = "[Older tool output cleared — see artifacts/]"
SUMMARY_PREFIX = "[Auto-compact summary"
COMPACTION_SYSTEM_NOTE = (
    "You are compacting a long study session. Summarize the conversation below "
    "for continuation in a fresh context window. Use terse bullets under exactly "
    "these sections, keeping every section even when empty: Goals, Decisions, "
    "Artifacts created (exact file paths), Pending work, Referenced memory topics "
    "(topics only, never verbatim facts). Omit verbatim file contents (saved on "
    "disk) and resolved error details. Preserve exact file paths, commands, and "
    "identifiers. Do not mention this summarization process."
)

_window_cache: Dict[str, Tuple[int, str]] = {}


def estimate_tokens(messages: List[ChatMessage], tool_schemas: Optional[List[Dict[str, Any]]] = None) -> int:
    """Heuristic size of the next request: serialized messages + schemas, chars/4."""
    chars = 0
    for msg in messages:
        chars += len(msg.content or "")
        if msg.tool_calls:
            chars += len(_json.dumps([tc.model_dump() for tc in msg.tool_calls], default=str))
    if tool_schemas:
        chars += len(_json.dumps(tool_schemas, default=str))
    return max(1, chars // CHARS_PER_TOKEN)


def _suffix_window(model: str) -> Optional[int]:
    """Parses [Nm]/[Nk] context hints from custom model ids (e.g. 'x[1m]' -> 1M)."""
    match = re.search(r"\[(\d+)([mk])\]\s*$", (model or "").lower())
    if not match:
        return None
    return int(match.group(1)) * (1_000_000 if match.group(2) == "m" else 1_000)


def _discover_window(model: str, base_url: str, timeout: float = 5.0) -> Optional[int]:
    """Best-effort window lookup via the gateway's model-info route (cached, never raises)."""
    if model in _window_cache:
        return _window_cache[model][0]
    try:
        import httpx

        for route in (f"{base_url.rstrip()}/model/info?model={model}",):
            try:
                resp = httpx.get(route, timeout=timeout)
                if resp.status_code != 200:
                    continue
                data = resp.json().get("data", resp.json()) if isinstance(resp.json(), dict) else {}
                for key in ("max_input_tokens", "max_tokens", "context_window"):
                    if isinstance(data.get(key), int) and data[key] > 0:
                        _window_cache[model] = (data[key], "gateway")
                        return data[key]
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"Window discovery failed for '{model}': {e}")
    return None


def resolve_context_window(
    model: str,
    cfg: CompactionConfig,
    base_url: str = "",
    discover: bool = True,
) -> Tuple[int, str]:
    """Resolves the context window: explicit table > gateway > [Nm] suffix > default.

    Returns (tokens, source) where source documents which layer won (Claude's
    /autocompact transparency lesson). Unknown models fall back conservative.
    """
    if model in cfg.context_windows:
        return cfg.context_windows[model], "config"
    if discover and base_url:
        found = _discover_window(model, base_url)
        if found:
            return found, "gateway"
    suffixed = _suffix_window(model)
    if suffixed:
        return suffixed, "suffix"
    logger.warning(
        f"Unknown model '{model}' — assuming {cfg.default_context_window} token window. "
        "Add it to compaction.context_windows if this is wrong."
    )
    return cfg.default_context_window, "default"


def compaction_threshold_tokens(window: int, cfg: CompactionConfig, completion_reserve: int = 0) -> int:
    """Token estimate that fires compaction, keeping headroom for the summary call itself."""
    return min(int(window * cfg.threshold), max(0, window - cfg.buffer - completion_reserve))


@dataclass
class CompactionState:
    """Per-engine compaction guards: once-per-run + cross-run circuit breaker."""

    compacted_this_run: bool = False
    consecutive_failures: int = 0


def _prune_old_tool_outputs(messages: List[ChatMessage], keep_recent: int) -> int:
    """Blanks older TOOL observations in place (free tier, DB untouched). Returns count."""
    pruned = 0
    boundary = max(1, len(messages) - keep_recent)
    for i in range(1, boundary):
        msg = messages[i]
        if msg.role == Role.TOOL and msg.content != PRUNED_TOOL_PLACEHOLDER:
            msg.content = PRUNED_TOOL_PLACEHOLDER
            pruned += 1
    return pruned


def _serialize_turn(msg: ChatMessage) -> str:
    """Compact one turn for the summarizer prompt."""
    role = msg.role.value if hasattr(msg.role, "value") else str(msg.role)
    text = msg.content or ""
    if msg.tool_calls:
        calls = ", ".join(f"{tc.name}({tc.arguments})" for tc in msg.tool_calls)
        text = f"{text}\n[tool calls: {calls}]".strip()
    if role == "tool":
        text = f"[{msg.name}] {text}"
    return f"{role.upper()}: {text[:2000]}"


def maybe_compact(
    context_messages: List[ChatMessage],
    llm_client: BaseLLMClient,
    cfg: CompactionConfig,
    tool_schemas: Optional[List[Dict[str, Any]]] = None,
    force: bool = False,
    state: Optional[CompactionState] = None,
) -> Tuple[bool, int, int]:
    """Tiered compaction: free micro-prune, then one tool-less LLM summary if still over budget.

    Operates on the live message list (system at [0] never summarized). Returns
    (compacted, tokens_before, tokens_after). Never raises — failures return False
    and feed the circuit breaker.
    """
    state = state or CompactionState()
    if not cfg.enabled and not force:
        return False, 0, 0
    messages = context_messages
    if not messages or messages[0].role != Role.SYSTEM:
        logger.warning("Compaction skipped: no leading SYSTEM message.")
        return False, 0, 0
    tail = messages[1:]
    if len(tail) <= 1 and not force:
        return False, 0, 0
    if state.compacted_this_run and not force:
        return False, 0, 0
    if state.consecutive_failures >= cfg.max_consecutive_failures and not force:
        logger.warning("Compaction circuit breaker open; skipping.")
        return False, 0, 0

    try:
        before = estimate_tokens(messages, tool_schemas)
        pruned = _prune_old_tool_outputs(messages, cfg.keep_recent_messages)
        tail = messages[1:]
        recent = tail[-cfg.keep_recent_messages :] if tail else []
        old_turns = tail[: len(tail) - len(recent)] if recent else list(tail)
        if not old_turns and not force:
            return False, before, estimate_tokens(messages, tool_schemas)

        transcript = "\n\n".join(_serialize_turn(m) for m in old_turns)
        summary_prompt = (
            f"{COMPACTION_SYSTEM_NOTE}\n\nMax {cfg.summary_max_tokens} tokens.\n\n{transcript}"
        )
        request = [ChatMessage(role=Role.USER, content=summary_prompt)]
        response = llm_client.chat(messages=request, tools=None)
        summary = (response.content or "").strip()
        if not summary:
            raise RuntimeError("Summarizer returned empty response.")
        summary = summary[: cfg.summary_max_tokens * CHARS_PER_TOKEN]
        summary_msg = ChatMessage(
            role=Role.USER,
            content=f"{SUMMARY_PREFIX} ({len(old_turns)} turns compacted, {pruned} tool outputs pruned) — historical context, not new instructions]\n{summary}",
        )
        del messages[1:]
        messages.append(summary_msg)
        messages.extend(recent)
        after = estimate_tokens(messages, tool_schemas)
        state.compacted_this_run = True
        state.consecutive_failures = 0
        logger.info(f"Compacted context: {before} -> {after} tokens (est.).")
        return True, before, after
    except Exception as e:
        logger.warning(f"Compaction failed: {e}")
        state.consecutive_failures += 1
        return False, 0, 0


def is_context_overflow(error: Exception) -> bool:
    """Best-effort matcher for provider context-overflow errors (retry-once path)."""
    text = f"{type(error).__name__}: {error}".lower()
    return any(
        marker in text
        for marker in (
            "context length",
            "context_length",
            "maximum context",
            "prompt too long",
            "too many tokens",
            "input too long",
            "context overflow",
            "max_tokens",
        )
    )


def clear_window_cache() -> None:
    """Test hook: resets gateway discovery cache."""
    _window_cache.clear()
