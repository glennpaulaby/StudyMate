import json
from pathlib import Path

import pytest

from study_agent.tools.base import ToolRegistry
from study_agent.tools.mcp.services import ObsidianVaultService
from study_agent.tools.mcp.tools import register_mcp_tools


def test_obsidian_service_blocks_escape_and_preserves_notes(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "concepts").mkdir()
    (vault / "concepts" / "graphs.md").write_text("# Graphs\n", encoding="utf-8")
    service = ObsidianVaultService(vault)

    notes = json.loads(service.list_notes())
    assert notes["notes"] == ["concepts/graphs.md"]
    assert (
        json.loads(service.read_note("concepts/graphs.md"))["content"] == "# Graphs\n"
    )

    with pytest.raises(PermissionError):
        service.read_note("../outside.md")
    with pytest.raises(FileExistsError):
        service.write_note("concepts/graphs.md", "replace")
    assert json.loads(
        service.edit_note("concepts/graphs.md", "# Graphs", "# Graphs\n\n[[Algorithms]]")
    )["edited"]
    assert "[[Algorithms]]" in (vault / "concepts" / "graphs.md").read_text(
        encoding="utf-8"
    )
    with pytest.raises(ValueError, match="exactly once"):
        service.edit_note("concepts/graphs.md", "missing", "replacement")


def test_mcp_tools_are_available_to_the_agent_registry(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_mcp_tools(registry, tmp_path)

    names = {schema["function"]["name"] for schema in registry.list_schemas()}
    assert names == {
        "web_search",
        "fetch_web_content",
        "expand_web_link",
        "obsidian_list_notes",
        "obsidian_read_note",
        "obsidian_search_notes",
        "obsidian_write_note",
        "obsidian_append_note",
        "obsidian_edit_note",
        "obsidian_get_outgoing_links",
        "obsidian_get_backlinks",
        "obsidian_insert_into_section",
    }

def test_obsidian_outgoing_links_and_backlinks(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    concepts = vault / "concepts"
    concepts.mkdir()
    lectures = vault / "lectures"
    lectures.mkdir()

    graph_note = concepts / "graphs.md"
    graph_note.write_text(
        "# Graphs\n\nRelated to [[Algorithms|algo]] and [[trees#properties]]. Also [[missing]].\n",
        encoding="utf-8",
    )

    lec_note = lectures / "lecture-01.md"
    lec_note.write_text(
        "# Lecture 01\n\nIntroduced [[Graphs]] and [[Data Structures]].\n",
        encoding="utf-8",
    )

    trees_note = concepts / "trees.md"
    trees_note.write_text(
        "# Trees\n\nA tree is an acyclic [[concepts/graphs.md]] instance.\n",
        encoding="utf-8",
    )

    service = ObsidianVaultService(vault)

    # Test outgoing links from graphs.md
    outgoing_res = json.loads(service.get_outgoing_links("concepts/graphs.md"))
    assert outgoing_res["path"] == "concepts/graphs.md"
    assert outgoing_res["outgoing_links"] == ["Algorithms", "trees", "missing"]
    assert outgoing_res["count"] == 3

    # Test backlinks targeting graphs.md
    backlinks_res = json.loads(service.get_backlinks("concepts/graphs.md"))
    assert backlinks_res["path"] == "concepts/graphs.md"
    assert sorted(backlinks_res["backlinks"]) == [
        "concepts/trees.md",
        "lectures/lecture-01.md",
    ]
    assert backlinks_res["count"] == 2


def test_obsidian_insert_into_section(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    note_path = vault / "graph.md"
    note_path.write_text(
        "# Graph\n\n## Key Concepts\n- Node\n- Edge\n\n## Open Questions\n- [ ] Question 1\n\n## Summary\nInitial summary.\n",
        encoding="utf-8",
    )

    service = ObsidianVaultService(vault)

    # Insert into middle section
    res1 = json.loads(
        service.insert_into_section("graph.md", "## Open Questions", "- [ ] Question 2")
    )
    assert res1["inserted"] is True

    content1 = note_path.read_text(encoding="utf-8")
    assert "- [ ] Question 1\n- [ ] Question 2\n\n## Summary" in content1

    # Insert into final section using clean heading name without leading #
    res2 = json.loads(
        service.insert_into_section("graph.md", "Summary", "Additional summary notes.")
    )
    assert res2["inserted"] is True

    content2 = note_path.read_text(encoding="utf-8")
    assert "Additional summary notes." in content2
    assert content2.endswith("\n")

    # Missing heading raises ValueError
    with pytest.raises(ValueError, match="not found in note"):
        service.insert_into_section("graph.md", "Nonexistent Section", "some content")
