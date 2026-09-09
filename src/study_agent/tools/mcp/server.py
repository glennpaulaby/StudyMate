"""MCP server exposing internet research and Obsidian vault tools."""

from __future__ import annotations

import os
from pathlib import Path

from study_agent.tools.mcp.services import ObsidianVaultService, WebResearchService


def create_mcp_server(
    vault_path: str | Path | None = None,
    search_service: WebResearchService | None = None,
):
    """Builds a FastMCP server with the two StudyMate capability groups."""
    from mcp.server.fastmcp import FastMCP

    research = search_service or WebResearchService()
    vault = ObsidianVaultService(
        vault_path or os.getenv("OBSIDIAN_VAULT_PATH", "./workspace/")
    )
    server = FastMCP("StudyMate Research and Obsidian")

    @server.tool()
    def research_topic(
        topic: str,
        max_results: int = 5,
        domains: list[str] | None = None,
        depth: int = 1,
        max_pages: int = 10,
    ) -> str:
        """Search multiple websites and optionally dive into linked pages."""
        if depth:
            return research.deep_search(topic, max_results, domains, depth, max_pages)
        return research.search(topic, max_results, domains)

    @server.tool()
    def obsidian_list_notes(prefix: str = "", limit: int = 100) -> str:
        """List Markdown notes in the configured Obsidian vault."""
        return vault.list_notes(prefix, limit)

    @server.tool()
    def obsidian_read_note(path: str) -> str:
        """Read a Markdown note from the configured Obsidian vault."""
        return vault.read_note(path)

    @server.tool()
    def obsidian_search_notes(query: str, limit: int = 20) -> str:
        """Search Markdown note content in the configured Obsidian vault."""
        return vault.search_notes(query, limit)

    @server.tool()
    def obsidian_write_note(path: str, content: str, overwrite: bool = False) -> str:
        """Create or explicitly overwrite a Markdown note. Deletion is not supported."""
        return vault.write_note(path, content, overwrite)

    @server.tool()
    def obsidian_edit_note(path: str, target: str, replacement: str) -> str:
        """Replace one unique text fragment in an existing Markdown note."""
        return vault.edit_note(path, target, replacement)

    @server.tool()
    def obsidian_append_note(path: str, content: str) -> str:
        """Append text to a Markdown note in the configured Obsidian vault."""
        return vault.append_note(path, content)

    @server.tool()
    def obsidian_get_outgoing_links(path: str) -> str:
        """Extract all [[wikilinks]] from a Markdown note."""
        return vault.get_outgoing_links(path)

    @server.tool()
    def obsidian_get_backlinks(path: str) -> str:
        """Find all notes in the vault that link to the specified note via [[wikilinks]]."""
        return vault.get_backlinks(path)

    @server.tool()
    def obsidian_insert_into_section(path: str, heading: str, content: str) -> str:
        """Insert content at the end of a specific Markdown section/heading."""
        return vault.insert_into_section(path, heading, content)

    return server


def main() -> None:
    """Run the MCP server over stdio for Claude, Cursor, or another MCP host."""
    create_mcp_server().run(transport="stdio")


if __name__ == "__main__":
    main()
