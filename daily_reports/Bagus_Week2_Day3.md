# My contributions:
- feature: Upgraded the MCP web research service parser to convert crawled HTML into structured Markdown (preserving headings, nested ordered/unordered lists, blockquotes, code blocks, and links) instead of flat text
- feature: Added knowledge graph navigation to the Obsidian MCP service with `obsidian_get_outgoing_links` to extract all `[[wikilinks]]` while cleaning display aliases and section anchors
- feature: Added `obsidian_get_backlinks` to identify all vault notes referencing a target note by slug, stem, filename, or relative path
- feature: Added section-aware note editing with `obsidian_insert_into_section` to reliably insert content into specific Markdown heading sections without breaking template structure or risking ambiguous string matches
- integration: Exposed the new outgoing links, backlinks, and section insertion tools across both the stdio FastMCP server (`server.py`) and StudyMate's local agent `ToolRegistry` (`tools.py`)
- test: Added unit tests covering HTML-to-Markdown parsing fidelity, outgoing wikilink extraction, cross-note backlinks discovery, section insertion, and tool registry discovery
- validation: Verified that all 116 tests in the test suite pass cleanly

# Notes:
- `_WebPageParser` strips non-content tags (`<script>`, `<style>`, `<nav>`, `<footer>`, `<header>`, `<aside>`, `<form>`) while preserving link targets for same-site crawling
- `obsidian_insert_into_section` accepts headings with or without leading `#` marks and strictly verifies heading existence before modifying the note
- The new graph and section tools maintain sandbox path containment and the vault no-delete guarantee
