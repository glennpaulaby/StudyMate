import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple
import yaml

logger = logging.getLogger(__name__)

PROCEDURES_DIRNAME = "procedures"
APPROVED = "approved"
DRAFT = "draft"
ARCHIVED = "archived"

PROCEDURE_TEMPLATE = """---
name: "{name}"
triggers: [{triggers}]
scope: "{scope}"
version: {version}
status: {status}
supersedes: {supersedes}
---

## Steps
{steps}

## Rationale
{rationale}

## Pitfalls
{pitfalls}

## Example
{example}
"""


@dataclass
class Procedure:
    """Parsed procedure bundle (shape, not just verbs)."""
    name: str
    triggers: List[str] = field(default_factory=list)
    scope: str = ""
    version: int = 1
    status: str = DRAFT
    supersedes: str = ""
    steps: str = ""
    rationale: str = ""
    pitfalls: str = ""
    example: str = ""
    source: str = ""


def format_procedure(
    name: str,
    steps: str,
    triggers: Optional[List[str]] = None,
    scope: str = "Describe when this routine applies and when it does not.",
    rationale: str = "Explain why each non-obvious step exists.",
    pitfalls: str = "- Stale steps after conventions change.",
    example: str = "A concrete past run that followed this routine.",
    version: int = 1,
    status: str = DRAFT,
    supersedes: str = "",
) -> str:
    """Formats a procedure bundle with frontmatter + shape sections."""
    triggers_str = ", ".join(f'"{t}"' for t in (triggers or []))
    return PROCEDURE_TEMPLATE.format(
        name=name,
        triggers=triggers_str,
        scope=scope,
        version=version,
        status=status,
        supersedes=f'"{supersedes}"' if supersedes else '""',
        steps=steps,
        rationale=rationale,
        pitfalls=pitfalls,
        example=example,
    )


def parse_procedure_file(path: Path) -> Optional[Procedure]:
    """Parses one procedure file; None when unreadable or frontmatter-less."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception as e:
        logger.warning(f"Skipping unreadable procedure file '{path}': {e}")
        return None
    if not text.startswith("---"):
        return None
    try:
        _, frontmatter_raw, body = text.split("---", 2)
        fm = yaml.safe_load(frontmatter_raw) or {}
    except Exception as e:
        logger.warning(f"Skipping malformed procedure file '{path}': {e}")
        return None
    if not isinstance(fm, dict) or not fm.get("name"):
        return None

    def _section(title: str) -> str:
        marker = f"## {title}\n"
        if marker not in body:
            return ""
        chunk = body.split(marker, 1)[1]
        for other in ("## Steps", "## Rationale", "## Pitfalls", "## Example"):
            if other != marker.rstrip("\n") and other in chunk:
                chunk = chunk.split(other, 1)[0]
        return chunk.strip()

    triggers = fm.get("triggers", [])
    if isinstance(triggers, str):
        triggers = [triggers]
    try:
        version = int(fm.get("version", 1))
    except (TypeError, ValueError):
        version = 1
    return Procedure(
        name=str(fm["name"]).strip().lower(),
        triggers=[str(t).strip().lower() for t in triggers if str(t).strip()],
        scope=str(fm.get("scope", "") or ""),
        version=version,
        status=str(fm.get("status", DRAFT) or DRAFT).strip().lower(),
        supersedes=str(fm.get("supersedes", "") or ""),
        steps=_section("Steps"),
        rationale=_section("Rationale"),
        pitfalls=_section("Pitfalls"),
        example=_section("Example"),
        source=str(path),
    )


def load_procedures(procedures_dir: Path | str) -> List[Procedure]:
    """Loads all parseable procedures from a directory (missing dir -> [])."""
    directory = Path(procedures_dir)
    if not directory.is_dir():
        return []
    procedures = []
    for file_path in sorted(directory.glob("*.md")):
        parsed = parse_procedure_file(file_path)
        if parsed is not None:
            procedures.append(parsed)
    return procedures


def match_procedures(
    procedures_dir: Path | str,
    user_text: str,
    statuses: Tuple[str, ...] = (APPROVED,),
    limit: int = 2,
    max_chars: int = 4000,
) -> List[Procedure]:
    """Matches procedures by substring over name/triggers/scope (case-insensitive).

    Only `statuses` match (archived never does); sorted by name for
    determinism; total injected text capped at max_chars.
    """
    query = (user_text or "").strip().lower()
    if not query:
        return []
    matched = []
    for proc in load_procedures(procedures_dir):
        if proc.status not in statuses or proc.status == ARCHIVED:
            continue
        haystacks = [proc.name] + proc.triggers + ([proc.scope] if proc.scope else [])
        if any(query in hay or hay in query for hay in haystacks if hay):
            matched.append(proc)
    matched.sort(key=lambda p: p.name)
    chosen: List[Procedure] = []
    budget = max_chars
    for proc in matched:
        if len(chosen) >= limit:
            break
        text = procedure_injection_text(proc)
        if len(text) > budget:
            break
        budget -= len(text)
        chosen.append(proc)
    return chosen


def procedure_injection_text(proc: Procedure) -> str:
    """Renders one procedure for prompt injection (steps + rationale, compact)."""
    lines = [f"Routine '{proc.name}' (v{proc.version}) — follow before improvising:"]
    if proc.scope:
        lines.append(f"Applies when: {proc.scope}")
    if proc.steps:
        lines.append(f"Steps:\n{proc.steps}")
    if proc.rationale:
        lines.append(f"Why: {proc.rationale}")
    if proc.pitfalls:
        lines.append(f"Pitfalls: {proc.pitfalls}")
    return "\n".join(lines)


def format_procedures_block(procedures: List[Procedure]) -> str:
    """Renders matched routines as one injectable block (empty list -> '')."""
    if not procedures:
        return ""
    parts = ["Applicable routines (follow before improvising):"]
    parts += [procedure_injection_text(p) for p in procedures]
    return "\n\n".join(parts)
