"""
Obsidian Markdown Knowledge Base Templates for StudyMate.

Defines standardized templates and helper formatters for generating linked
Obsidian study artifacts, including Concept Notes, Lecture Summaries, and
Flashcard Decks with YAML frontmatter, wikilinks, and Open Questions.
"""

from datetime import date
from typing import List, Optional


CONCEPT_TEMPLATE = """---
title: "{title}"
tags: [{tags}]
course: "{course}"
status: {status}
last_updated: {last_updated}
prerequisites: [{prerequisites}]
---

## One-line Summary
*{one_line_summary}*

## Core Intuition
{core_intuition}

## Formal Definition / Statement
{formal_definition}

## Key Properties / Complexity
{key_properties}

## Worked Example
{worked_example}

## Common Pitfalls
{common_pitfalls}

## Connections
{connections}

## Open Questions
{open_questions}
"""


LECTURE_TEMPLATE = """---
title: "{title}"
course: "{course}"
date: {date}
tags: [lecture, {course_tag}]
---

# {title}

## Key Takeaways
{takeaways}

## Linked Concepts
{linked_concepts}

## Detailed Synthesis
{synthesis}
"""


FLASHCARD_TEMPLATE = """# Flashcards: {topic}

{cards}
"""


def format_concept_note(
    title: str,
    course: str,
    one_line_summary: str,
    core_intuition: str,
    formal_definition: str,
    key_properties: str = "- Time Complexity: O(1)\n- Space Complexity: O(1)",
    worked_example: str = "Step-by-step walkthrough.",
    common_pitfalls: str = "- Failing to consider edge cases.",
    connections: Optional[List[str]] = None,
    open_questions: Optional[List[str]] = None,
    tags: Optional[List[str]] = None,
    prerequisites: Optional[List[str]] = None,
    status: str = "current"
) -> str:
    """
    Formats a standardized Obsidian concept note with wikilinks and YAML frontmatter.

    Args:
        title: Concept title (e.g., 'Transformer Self-Attention').
        course: Course name or code (e.g., 'Deep Learning').
        one_line_summary: High-level concise takeaway.
        core_intuition: Conceptual breakdown explaining the 'why'.
        formal_definition: Mathematical equations or formal specifications.
        key_properties: Asymptotic bounds, complexity, or structural properties.
        worked_example: Concrete step-by-step application.
        common_pitfalls: Common student misconceptions or failure modes.
        connections: List of note names to link via `[[wikilinks]]`.
        open_questions: List of unresolved questions formatted as `- [ ]`.
        tags: List of tags for Obsidian indexing.
        prerequisites: List of prerequisite concept names.
        status: Note lifecycle status ('current', 'evergreen', 'draft').

    Returns:
        Rendered markdown string ready to be saved to `workspace/artifacts/`.
    """
    tags_str = ", ".join(tags) if tags else f"concept, {course.lower().replace(' ', '-')}"
    prereqs_str = ", ".join(f'"{p}"' for p in (prerequisites or []))
    
    conn_str = "\n".join(f"- [[{c}]] — Relationship description." for c in (connections or []))
    if not conn_str:
        conn_str = "*(No explicit connections logged yet)*"

    oq_str = "\n".join(f"- [ ] {q}" for q in (open_questions or []))
    if not oq_str:
        oq_str = "*(None logged)*"

    return CONCEPT_TEMPLATE.format(
        title=title,
        tags=tags_str,
        course=course,
        status=status,
        last_updated=date.today().isoformat(),
        prerequisites=prereqs_str,
        one_line_summary=one_line_summary,
        core_intuition=core_intuition,
        formal_definition=formal_definition,
        key_properties=key_properties,
        worked_example=worked_example,
        common_pitfalls=common_pitfalls,
        connections=conn_str,
        open_questions=oq_str
    )

