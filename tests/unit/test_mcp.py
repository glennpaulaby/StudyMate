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
    }
