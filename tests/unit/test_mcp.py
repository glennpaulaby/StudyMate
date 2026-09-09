import json
from pathlib import Path

import httpx
import pytest

from study_agent.tools.base import ToolRegistry
from study_agent.tools.mcp.services import ObsidianVaultService, WebResearchService
from study_agent.tools.mcp.tools import register_mcp_tools


class FakeSearchClient:
    def __init__(self, body: str, pages: dict[str, str] | None = None) -> None:
        self.body = body
        self.pages = pages or {}

    def get(self, url: str) -> httpx.Response:
        body = next(
            (content for page_url, content in self.pages.items() if url.startswith(page_url)),
            self.body,
        )
        return httpx.Response(
            200,
            text=body,
            headers={"content-type": "text/html"},
            request=httpx.Request("GET", url),
        )


def test_research_topic_aggregates_and_deduplicates_sources() -> None:
    html = """
    <a class="result__a" href="https://example.com/a">First source</a>
    <div class="result__snippet">A useful explanation.</div>
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa">Duplicate</a>
    <div class="result__snippet">Duplicate source.</div>
    <a class="result__a" href="https://example.com/b">Second source</a>
    <div class="result__snippet">Another explanation.</div>
    """
    service = WebResearchService(client=FakeSearchClient(html))

    result = json.loads(service.search("dynamic programming", max_results=5))

    assert result["topic"] == "dynamic programming"
    assert result["source_count"] == 2
    assert [source["url"] for source in result["sources"]] == [
        "https://example.com/a",
        "https://example.com/b",
    ]


def test_deep_research_searches_each_domain_and_follows_same_site_links() -> None:
    search_html = """
    <a class="result__a" href="https://example.com/start">Example</a>
    <div class="result__snippet">Example source.</div>
    """
    page_html = """
    <html><body><h1>Deep topic</h1><p>Important context.</p>
    <a href="/details">Details</a><a href="https://other.example/ignore">Ignore</a>
    </body></html>
    """
    details_html = "<html><body><p>More context.</p></body></html>"
    service = WebResearchService(
        client=FakeSearchClient(
            search_html,
            {
                "https://example.com/start": page_html,
                "https://example.com/details": details_html,
            },
        )
    )

    result = json.loads(
        service.deep_search(
            "graph algorithms", max_results=2, domains=["example.com", "docs.example.com"]
        )
    )

    assert result["queries"] == [
        "graph algorithms site:example.com",
        "graph algorithms site:docs.example.com",
    ]
    assert result["page_count"] == 2
    assert [page["url"] for page in result["sources"][0]["pages"]] == [
        "https://example.com/start",
        "https://example.com/details",
    ]
    assert "Important context." in result["sources"][0]["pages"][0]["content"]


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
    register_mcp_tools(
        registry, tmp_path, WebResearchService(client=FakeSearchClient(""))
    )

    names = {schema["function"]["name"] for schema in registry.list_schemas()}
    assert names == {
        "research_topic",
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


def test_web_page_parser_converts_html_to_structured_markdown() -> None:
    from study_agent.tools.mcp.services import _WebPageParser

    html = """
    <html>
    <head><style>body { color: red; }</style></head>
    <body>
        <script>console.log("ignore");</script>
        <h1>Main Topic</h1>
        <p>This is <strong>important</strong> and <em>notable</em>.</p>
        <h2>Key Concepts</h2>
        <ul>
            <li>First point with <code>inline_code()</code></li>
            <li>Second point</li>
        </ul>
        <pre><code>def solve():
    return 42</code></pre>
        <p>Reference: <a href="https://example.com/docs">Documentation</a></p>
    </body>
    </html>
    """
    parser = _WebPageParser()
    parser.feed(html)
    markdown = parser.get_markdown()

    assert "# Main Topic" in markdown
    assert "## Key Concepts" in markdown
    assert "**important**" in markdown
    assert "*notable*" in markdown
    assert "- First point with `inline_code()`" in markdown
    assert "```\ndef solve():\n    return 42\n```" in markdown
    assert "[Documentation](https://example.com/docs)" in markdown
    assert "console.log" not in markdown
    assert "color: red" not in markdown
    assert parser.links == ["https://example.com/docs"]


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

