import json
from pathlib import Path

import httpx
import pytest

from study_agent.tools.base import ToolRegistry
from study_agent.tools.mcp.services import ObsidianVaultService, WebResearchService
from study_agent.tools.mcp.tools import register_mcp_tools


class FakeSearchClient:
    def __init__(self, body: str) -> None:
        self.body = body

    def get(self, url: str) -> httpx.Response:
        return httpx.Response(200, text=self.body, request=httpx.Request("GET", url))


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
    }
