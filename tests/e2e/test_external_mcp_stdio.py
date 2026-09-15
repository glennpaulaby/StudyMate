"""End-to-end: one persistent stdio session against the real external server.

Runs the actual duckduckgo-mcp-server binary (prebaked in the image at
/opt/mcp/ddg/bin, or any `duckduckgo-mcp-server` on PATH). Asserts the
Week-2 MCP integration contract against a live server: discover tools at
runtime, invoke a discovered tool, append its result to context. Skips when
the binary is unavailable so bare checkouts stay runnable.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from study_agent.config.settings import MCPToolConfig
from study_agent.core.context import ContextManager
from study_agent.core.types import Role
from study_agent.tools.base import ToolRegistry
from study_agent.tools.mcp.external import ExternalMCPService
from study_agent.tools.mcp.tools import register_mcp_tools


def _resolve_command() -> str:
    scripts_dir = Path(sys.executable).parent  # covers .venv and /opt/mcp/ddg/bin
    candidates = [
        MCPToolConfig.model_fields["search_command"].default,
        str(scripts_dir / "duckduckgo-mcp-server"),
        "duckduckgo-mcp-server",
    ]
    for cand in candidates:
        found = shutil.which(cand)
        if found:
            return found
    raise pytest.skip("duckduckgo-mcp-server binary not installed")


@pytest.fixture(scope="module")
def external_service() -> ExternalMCPService:
    service = ExternalMCPService(command=_resolve_command(), args=[])
    yield service
    service.close()


def test_runtime_tool_discovery_finds_the_search_tool(external_service) -> None:
    """list_tools() on the live server exposes 'search' and 'fetch_content'."""

    async def probe() -> list[str]:
        server = StdioServerParameters(
            command=external_service.command, args=[], env=None
        )
        async with stdio_client(server) as (read, write), ClientSession(
            read, write
        ) as session:
            await session.initialize()
            tools = await session.list_tools()
            return [tool.name for tool in tools.tools]

    names = asyncio.run(probe())
    assert "search" in names
    assert "fetch_content" in names


def test_web_search_results_are_appended_to_context(external_service, tmp_path) -> None:
    """A registry-dispatched web_search call returns JSON and enters context."""
    registry = ToolRegistry()
    register_mcp_tools(registry, vault_path=tmp_path, external_service=external_service)

    result = registry.execute("web_search", {"query": "Python programming language", "max_results": 3})

    assert isinstance(result, str) and result.strip()
    context = ContextManager(custom_system_prompt="test persona")
    tool_msg = context.add_tool_result("call_live_1", "web_search", result)
    assert context.get_messages()[-1].role == Role.TOOL
    assert context.get_messages()[-1].content == result


def test_session_survives_across_two_calls(external_service) -> None:
    """Two consecutive calls reuse the one warm session (the per-call install bug)."""
    first = external_service.call_tool(
        "search", {"query": "what is a graph data structure", "max_results": 2}
    )
    assert external_service._session is not None  # worker still connected
    session_after_first = external_service._session
    second = external_service.call_tool(
        "search", {"query": "what is a binary tree", "max_results": 2}
    )
    assert first.strip() and second.strip()
    assert external_service._session is session_after_first  # NOT respawned
