# My contributions:
- setup: Verified the nested `group-11` repository and created the requested branch `feat/search-and-obisidan-mcp`
- investigation: Confirmed that the referenced `demo-week1` branch did not exist locally or remotely, so the feature branch was based on the repository's current `main`
- feature: Added an internet research service that searches DuckDuckGo, parses result titles/URLs/snippets, normalizes redirect URLs, and removes duplicate sources
- feature: Added Obsidian vault services for listing, reading, searching, creating, overwriting, and appending Markdown notes
- security: Restricted Obsidian operations to the configured vault root, blocked path traversal, limited operations to Markdown files, refused accidental overwrites, and provided no deletion operation
- feature: Added a standalone stdio MCP server exposing `research_topic` and five Obsidian tools
- integration: Registered the MCP capabilities in StudyMate's existing `ToolRegistry` so the agent can use them during normal ReAct sessions
- configuration: Added MCP settings for enablement, search endpoint, result limits, and vault path
- configuration: Changed the default Obsidian vault from `workspace/artifacts` to the project `workspace/` directory
- dependency: Added and pinned the MCP SDK as `mcp>=1.9.0,<2.0` to use the implemented `FastMCP` API
- feature: Extended `/lint` so detected broken wikilinks and metadata/frontmatter issues trigger a constrained automatic repair pass through the model
- safety: Made the lint repair prompt read affected notes first, use existing safe file tools, preserve unrelated content, avoid inventing notes, and never delete files
- validation: Added MCP unit tests covering source aggregation, URL deduplication, vault traversal protection, overwrite protection, and tool registration
- validation: Ran the MCP, CLI, and vault-linter regression tests successfully
- validation: Performed a live search smoke test and confirmed that normalized web sources are returned
- documentation: Updated the README with MCP capabilities, launch instructions, vault configuration, and the current MCP roadmap status

# Notes:
- The standalone MCP server is launched with `python -m study_agent.tools.mcp`
- The default vault is `./workspace/`; `OBSIDIAN_VAULT_PATH` can override it
- `/lint` automatically repairs broken links and formatting/frontmatter issues, then runs a second lint pass
- Orphan pages and open questions are reported but are not automatically changed
- The full suite still contains one pre-existing configuration mismatch: the code default for `AgentConfig.max_iterations` is 60 while an existing test expects 15
