from pathlib import Path
from study_agent.study.linter import VaultLinter
from study_agent.study.templates import format_concept_note


def test_vault_linter_clean_vault(tmp_path: Path):
    """Verify VaultLinter passes on a healthy, cross-linked set of notes."""
    vault = tmp_path / "vault"
    vault.mkdir()

    # Note A links to Note B
    note_a = format_concept_note(
        title="Dijkstra Algorithm",
        course="Algorithms",
        one_line_summary="Shortest path algorithm for non-negative edge weights.",
        core_intuition="Greedy relaxation using a priority queue.",
        formal_definition="Given graph G and source s...",
        connections=["bellman-ford"]
    )
    (vault / "dijkstra.md").write_text(note_a, encoding="utf-8")

    # Note B links to Note A
    note_b = format_concept_note(
        title="Bellman-Ford Algorithm",
        course="Algorithms",
        one_line_summary="Shortest path algorithm handling negative edge weights.",
        core_intuition="Dynamic programming edge relaxation.",
        formal_definition="Given graph G...",
        connections=["dijkstra"]
    )
    (vault / "bellman-ford.md").write_text(note_b, encoding="utf-8")

    linter = VaultLinter(vault)
    report = linter.lint()

    assert report.total_files == 2
    assert report.total_links == 2
    assert len(report.broken_links) == 0
    assert len(report.orphan_pages) == 0
    assert report.is_healthy is True


def test_vault_linter_broken_wikilink(tmp_path: Path):
    """Verify VaultLinter detects references to non-existent notes."""
    vault = tmp_path / "vault"
    vault.mkdir()

    note = format_concept_note(
        title="QuickSort",
        course="Algorithms",
        one_line_summary="Divide and conquer sorting algorithm.",
        core_intuition="Partitioning around a pivot.",
        formal_definition="Recursive partitioning.",
        connections=["non-existent-sorting-note"]
    )
    (vault / "quicksort.md").write_text(note, encoding="utf-8")

    linter = VaultLinter(vault)
    report = linter.lint()

    assert len(report.broken_links) == 1
    assert "quicksort.md" in report.broken_links[0][0]
    assert "non-existent-sorting-note" in report.broken_links[0][1]


def test_vault_linter_orphan_page_detection(tmp_path: Path):
    """Verify VaultLinter detects notes that have no inbound links."""
    vault = tmp_path / "vault"
    vault.mkdir()

    # Note 1 has no inbound links
    (vault / "isolated_note.md").write_text(
        "---\ntitle: Isolated Note\nstatus: current\n---\n# Isolated Note\nNo links here.",
        encoding="utf-8"
    )

    linter = VaultLinter(vault)
    report = linter.lint()

    assert len(report.orphan_pages) == 1
    assert "isolated_note.md" in report.orphan_pages[0]


def test_vault_linter_open_questions_extraction(tmp_path: Path):
    """Verify VaultLinter surfaces pending open questions."""
    vault = tmp_path / "vault"
    vault.mkdir()

    note = format_concept_note(
        title="A-Star Search",
        course="AI",
        one_line_summary="Informed heuristic search algorithm.",
        core_intuition="f(n) = g(n) + h(n)",
        formal_definition="Best-first search...",
        open_questions=["What happens when heuristic h(n) is not admissible?"]
    )
    (vault / "a-star.md").write_text(note, encoding="utf-8")

    linter = VaultLinter(vault)
    report = linter.lint()

    assert len(report.open_questions) == 1
    assert "a-star.md" in report.open_questions[0][0]
    assert "heuristic h(n) is not admissible" in report.open_questions[0][1]
