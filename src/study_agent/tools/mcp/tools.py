"""ToolRegistry adapters for the internet and Obsidian MCP capabilities."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from study_agent.tools.base import BaseTool, ToolRegistry
from study_agent.tools.mcp.services import ObsidianVaultService, WebResearchService


class ResearchTopicTool(BaseTool):
    """Searches the public web and aggregates source metadata for a topic."""

    def __init__(self, service: WebResearchService, max_results: int = 5) -> None:
        self.service = service
        self.max_results = max_results

    @property
    def name(self) -> str:
        return "research_topic"

    @property
    def description(self) -> str:
        return "Search the internet for a study topic and return deduplicated sources with titles, URLs, and snippets."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "Topic to research"},
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "default": self.max_results,
                },
                "domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional domains to restrict results to",
                },
            },
            "required": ["topic"],
        }

    def execute(
        self,
        topic: str,
        max_results: int | None = None,
        domains: list[str] | None = None,
        **kwargs: Any,
    ) -> str:
        return self.service.search(topic, max_results or self.max_results, domains)


class _VaultTool(BaseTool):
    """Base class for tools operating on one configured Obsidian vault."""

    def __init__(self, service: ObsidianVaultService) -> None:
        self.service = service


class ListNotesTool(_VaultTool):
    @property
    def name(self) -> str:
        return "obsidian_list_notes"

    @property
    def description(self) -> str:
        return "List Markdown notes in the configured Obsidian vault."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "prefix": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "required": [],
        }

    def execute(self, prefix: str = "", limit: int = 100, **kwargs: Any) -> str:
        return self.service.list_notes(prefix, limit)


class ReadNoteTool(_VaultTool):
    @property
    def name(self) -> str:
        return "obsidian_read_note"

    @property
    def description(self) -> str:
        return "Read a Markdown note from the configured Obsidian vault."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        }

    def execute(self, path: str, **kwargs: Any) -> str:
        return self.service.read_note(path)


class SearchNotesTool(_VaultTool):
    @property
    def name(self) -> str:
        return "obsidian_search_notes"

    @property
    def description(self) -> str:
        return "Search Markdown note content in the configured Obsidian vault."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["query"],
        }

    def execute(self, query: str, limit: int = 20, **kwargs: Any) -> str:
        return self.service.search_notes(query, limit)


class WriteNoteTool(_VaultTool):
    @property
    def name(self) -> str:
        return "obsidian_write_note"

    @property
    def description(self) -> str:
        return "Create or explicitly overwrite a Markdown note in the configured Obsidian vault; deletion is not supported."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "overwrite": {"type": "boolean", "default": False},
            },
            "required": ["path", "content"],
        }

    def execute(
        self, path: str, content: str, overwrite: bool = False, **kwargs: Any
    ) -> str:
        return self.service.write_note(path, content, overwrite)


class AppendNoteTool(_VaultTool):
    @property
    def name(self) -> str:
        return "obsidian_append_note"

    @property
    def description(self) -> str:
        return "Append text to a Markdown note in the configured Obsidian vault."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        }

    def execute(self, path: str, content: str, **kwargs: Any) -> str:
        return self.service.append_note(path, content)


def register_mcp_tools(
    registry: ToolRegistry,
    vault_path: str | Path,
    search_service: WebResearchService | None = None,
    max_results: int = 5,
) -> None:
    """Registers both MCP capabilities as local agent tools."""
    registry.register(
        ResearchTopicTool(search_service or WebResearchService(), max_results)
    )
    vault = ObsidianVaultService(vault_path)
    registry.register(ListNotesTool(vault))
    registry.register(ReadNoteTool(vault))
    registry.register(SearchNotesTool(vault))
    registry.register(WriteNoteTool(vault))
    registry.register(AppendNoteTool(vault))
