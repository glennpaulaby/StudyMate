from abc import ABC, abstractmethod
import logging
from typing import Any, Dict, List, Optional

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
        """
        tool = self.get(name)
        if not tool:
            return f"Error: Tool '{name}' is not registered."

        try:
            return tool.execute(**arguments)
        except Exception as e:
            logger.error(f"Error executing tool '{name}' with args {arguments}: {e}")
            return f"Error executing tool '{name}': {str(e)}"
