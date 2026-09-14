from abc import ABC, abstractmethod
import logging
import time
from typing import Any, Dict, List, Optional

from study_agent.observability.metrics import (
    observe_component_latency,
    observe_tool,
)

logger = logging.getLogger(__name__)


class BaseTool(ABC):
    """
    Abstract base class for all agent tools.
    Provides schema definitions for function calling and an execution interface.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Tool name identifier."""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """Clear description of tool capabilities for LLM reasoning."""
        pass

    @property
    @abstractmethod
    def parameters(self) -> Dict[str, Any]:
        """JSON schema defining expected input arguments."""
        pass

    @abstractmethod
    def execute(self, **kwargs: Any) -> str:
        """
        Executes the tool with provided arguments and returns observation string.
        """
        pass

    def to_openai_schema(self) -> Dict[str, Any]:
        """Serializes tool metadata into OpenAI function calling format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def tool_source(tool: BaseTool) -> str:
    """Infers the observability source tier from the tool's home module.

    `builtin` = filesystem/PDF tools, `mcp` = web/vault MCP bridge,
    `memory` = long-term memory tools. Unknown modules default to builtin.
    """
    module = type(tool).__module__ or ""
    if ".tools.mcp" in module or module.endswith(".mcp"):
        return "mcp"
    if "memory" in module:
        return "memory"
    return "builtin"


class ToolRegistry:
    """
    Registry managing available tools, schema generation, and execution dispatch.
    """

    def __init__(self) -> None:
        self._tools: Dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """Registers a tool in the registry."""
        if tool.name in self._tools:
            logger.warning(f"Overwriting existing tool registration: {tool.name}")
        self._tools[tool.name] = tool

    @property
    def tools(self) -> Dict[str, BaseTool]:
        """Returns the dictionary of registered tools."""
        return self._tools

    def get(self, name: str) -> Optional[BaseTool]:
        """Retrieves a registered tool by name."""
        return self._tools.get(name)

    def list_schemas(self) -> List[Dict[str, Any]]:
        """Returns all registered tool schemas formatted for LLM function calling."""
        return [tool.to_openai_schema() for tool in self._tools.values()]

    def execute(self, name: str, arguments: Dict[str, Any]) -> str:
        """
        Executes a registered tool by name with provided arguments.
        Catches execution errors gracefully and returns descriptive error string.
        Emits tool-call metrics (no-op unless the metrics exporter is enabled).
        """
        tool = self.get(name)
        if not tool:
            observe_tool(name, source="builtin", status="not_found")
            return f"Error: Tool '{name}' is not registered."

        source = tool_source(tool)
        start = time.perf_counter()
        try:
            result = tool.execute(**arguments)
        except Exception as e:
            duration_s = time.perf_counter() - start
            observe_tool(name, source=source, status="error", duration_s=duration_s)
            observe_component_latency("tool", duration_s)
            logger.error(f"Error executing tool '{name}' with args {arguments}: {e}")
            return f"Error executing tool '{name}': {str(e)}"
        duration_s = time.perf_counter() - start
        # Convention: tools surface failures as "Error..." observation strings.
        status = "error" if result.startswith("Error") else "ok"
        observe_tool(name, source=source, status=status, duration_s=duration_s)
        observe_component_latency("tool", duration_s)
        return result
