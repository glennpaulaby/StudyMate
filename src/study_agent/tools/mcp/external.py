"""Synchronous bridge to an external MCP server started over stdio."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@dataclass
class ExternalMCPService:
    """Calls tools exposed by a configured external MCP server."""

    command: str = "uvx"
    args: list[str] = field(
        default_factory=lambda: [
            "--with",
            "duckduckgo-mcp-server[browser]",
            "duckduckgo-mcp-server",
        ]
    )
    env: dict[str, str] = field(
        default_factory=lambda: {"DDG_REF_URL_THRESHOLD": "0"}
    )

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Start the server, call one tool, and return its text content."""
        return asyncio.run(self._call_tool(name, arguments))

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        environment = os.environ.copy()
        environment.update(self.env)
        server = StdioServerParameters(
            command=self.command,
            args=self.args,
            env=environment,
        )
        async with stdio_client(server) as (read_stream, write_stream), ClientSession(
            read_stream, write_stream
        ) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments)

        content: list[str] = []
        for item in result.content:
            text = getattr(item, "text", None)
            if text:
                content.append(text)
        if content:
            return "\n".join(content)
        if getattr(result, "isError", False):
            return f"External MCP tool '{name}' returned an error without details."
        return "External MCP tool returned no text."
