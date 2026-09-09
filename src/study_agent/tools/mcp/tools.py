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
        return "Search multiple websites for a study topic and optionally follow same-site links for deeper context."

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
                    "description": "Optional domains; each domain is searched independently",
                },
                "depth": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 2,
                    "default": 1,
                    "description": "Same-site link depth; 0 returns search results only",
                },
                "max_pages": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 10,
                    "description": "Maximum pages to fetch per source during deep research",
                },
            },
            "required": ["topic"],
        }

    def execute(
        self,
        topic: str,
        max_results: int | None = None,
        domains: list[str] | None = None,
        depth: int = 1,
        max_pages: int = 10,
        **kwargs: Any,
    ) -> str:
        if depth:
            return self.service.deep_search(
                topic, max_results or self.max_results, domains, depth, max_pages
            )
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


class EditNoteTool(_VaultTool):
    @property
    def name(self) -> str:
        return "obsidian_edit_note"

    @property
    def description(self) -> str:
        return "Replace one unique text fragment in an existing Markdown note."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "target": {
                    "type": "string",
                    "description": "Exact existing text; it must occur exactly once",
                },
                "replacement": {"type": "string"},
            },
            "required": ["path", "target", "replacement"],
        }

    def execute(
        self, path: str, target: str, replacement: str, **kwargs: Any
    ) -> str:
        return self.service.edit_note(path, target, replacement)


class GetOutgoingLinksTool(_VaultTool):
    @property
    def name(self) -> str:
        return "obsidian_get_outgoing_links"

    @property
    def description(self) -> str:
        return "Extract all [[wikilinks]] from a Markdown note in the configured Obsidian vault."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Vault-relative path to the Markdown note",
                }
            },
            "required": ["path"],
        }

    def execute(self, path: str, **kwargs: Any) -> str:
        return self.service.get_outgoing_links(path)


class GetBacklinksTool(_VaultTool):
    @property
    def name(self) -> str:
        return "obsidian_get_backlinks"

    @property
    def description(self) -> str:
        return "Find all Markdown notes in the vault that link to the specified note via [[wikilinks]]."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Vault-relative path to the target note",
                }
            },
            "required": ["path"],
        }

    def execute(self, path: str, **kwargs: Any) -> str:
        return self.service.get_backlinks(path)


class InsertIntoSectionTool(_VaultTool):
    @property
    def name(self) -> str:
        return "obsidian_insert_into_section"

    @property
    def description(self) -> str:
        return "Insert content at the end of a specific Markdown section (heading) in an existing note."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Vault-relative path to the note",
                },
                "heading": {
                    "type": "string",
                    "description": "Section heading title, with or without leading '#'",
                },
                "content": {
                    "type": "string",
                    "description": "Content to insert at the end of the section",
                },
            },
            "required": ["path", "heading", "content"],
        }

    def execute(
        self, path: str, heading: str, content: str, **kwargs: Any
    ) -> str:
        return self.service.insert_into_section(path, heading, content)


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
    registry.register(EditNoteTool(vault))
    registry.register(GetOutgoingLinksTool(vault))
    registry.register(GetBacklinksTool(vault))
    registry.register(InsertIntoSectionTool(vault))
