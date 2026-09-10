"""External web-search and local Obsidian MCP capabilities."""

from study_agent.tools.mcp.external import ExternalMCPService
from study_agent.tools.mcp.services import ObsidianVaultService
from study_agent.tools.mcp.tools import register_mcp_tools

__all__ = ["ExternalMCPService", "ObsidianVaultService", "register_mcp_tools"]
