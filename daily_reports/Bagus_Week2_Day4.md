# My contributions:
- implemented: Replaced the in-process DuckDuckGo HTML scraper and `research_topic` tool with an external stdio MCP bridge for `nickclyde/duckduckgo-mcp-server`
- implemented: Added `ExternalMCPService` using the MCP Python client to start the external server through `uvx`, initialize a session, and call remote tools
- implemented: Exposed `web_search`, `fetch_web_content`, and `expand_web_link` to the StudyMate ReAct agent, enabling iterative web search and deep research
- refactor: Kept the Obsidian vault tools local and changed the standalone StudyMate MCP server to expose only Obsidian operations
- config: Added configurable external MCP command and arguments to `MCPToolConfig` and `config/config.yaml`; Docker now installs `uv`
- docs: Updated the README with the external DuckDuckGo MCP setup, launcher configuration, and deep-research workflow
- test: Updated unit and end-to-end MCP tests for the external-tool names and bridge behavior
- validation: Ran the full test suite with 133 tests passing; MCP-focused tests also passed

# Team:
- Replaced the custom web-search implementation with the requested external MCP integration while preserving the existing Obsidian MCP functionality
- Chose YAML as the StudyMate application configuration format; the external server is launched from the configured command rather than requiring a separate JSON MCP-host configuration
