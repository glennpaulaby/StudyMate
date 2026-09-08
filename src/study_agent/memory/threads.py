import re
import uuid
from typing import List, Optional

from study_agent.memory.store import ThreadRecord


def generate_thread_name(
    first_message: str,
    second_message: Optional[str] = None,
    max_len: int = 40,
    fallback_id: Optional[str] = None,
) -> str:
    """
    Generates a deterministic thread name from the first message(s).

    Heuristic v1 (no LLM call): slugify first user message, truncate.
    Falls back to `thread-<shortid>` when input is empty.
    """
    _ = second_message  # Reserved for v2 (first-couple-messages naming).
    text = re.sub(r"\s+", " ", (first_message or "").strip())
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    if not slug:
        short = fallback_id or uuid.uuid4().hex[:8]
        return f"thread-{short}"
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("-")
    return slug or f"thread-{fallback_id or uuid.uuid4().hex[:8]}"


def resolve_thread(
    threads: List[ThreadRecord], query: str
) -> Optional[ThreadRecord]:
    """
    Resolves a thread by exact id, id prefix, or case-insensitive name.

    Returns None when no match, first most-recent match on ambiguity
    (callers pass `list_threads()` which is already updated_at DESC).
    """
    q = (query or "").strip()
    if not q:
        return None
    for t in threads:
        if t.id == q:
            return t
    for t in threads:
        if t.id.startswith(q):
            return t
    q_lower = q.lower()
    for t in threads:
        if t.name.lower() == q_lower:
            return t
    return None
