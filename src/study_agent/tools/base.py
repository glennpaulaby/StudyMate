from abc import ABC, abstractmethod
import logging
from typing import Any, Callable, Dict, List, Optional

from study_agent.security.policy import (
    Decision,
    PermissionEvent,
    PermissionPolicy,
)

logger = logging.getLogger(__name__)

# Called with (tool_name, arguments, rule_description); returns True to proceed.
ConfirmationHandler = Callable[[str, Dict[str, Any], str], bool]


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

    When a PermissionPolicy is attached, every execute() — built-in and MCP
    tools alike — is gated by the policy before the tool runs. Denials and
    user rejections surface to the LLM as "Error: ..." observation strings,
    never as exceptions.
    """

    def __init__(
        self,
        policy: Optional[PermissionPolicy] = None,
        confirmation_handler: Optional[ConfirmationHandler] = None,
    ) -> None:
        self._tools: Dict[str, BaseTool] = {}
        self.policy = policy
        self.confirmation_handler = confirmation_handler
        # Lifecycle hook seam (Week 3 observability): listeners for permission events.
        self.on_permission_decision: List[Callable[[PermissionEvent], None]] = []

    def _emit(self, event: PermissionEvent) -> None:
        """Fires a permission event to all listeners; listener errors are contained."""
        for listener in self.on_permission_decision:
            try:
                listener(event)
            except Exception as e:  # never let telemetry break a tool call
                logger.warning(f"Permission event listener failed: {e}")

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
        Applies the permission policy first (allow / deny / require-user-confirmation).
        Catches execution errors gracefully and returns descriptive error string.
        """
        tool = self.get(name)
        if not tool:
            return f"Error: Tool '{name}' is not registered."

        arguments = arguments or {}
        gate = self._check_permission(name, arguments)
        if gate is not None:
            return gate

        try:
            return tool.execute(**arguments)
        except Exception as e:
            logger.error(f"Error executing tool '{name}' with args {arguments}: {e}")
            return f"Error executing tool '{name}': {str(e)}"

    def _check_permission(self, name: str, arguments: Dict[str, Any]) -> Optional[str]:
        """
        Returns None when the call may proceed, or the error observation string
        the LLM should receive when permission is refused. Fail-closed: with a
        policy attached, CONFIRM without a handler (tests, non-interactive runs)
        is treated as DENY.
        """
        if self.policy is None:
            return None

        decision_obj = self.policy.evaluate(name, arguments)
        decision = decision_obj.decision
        rule_desc = decision_obj.rule.describe() if decision_obj.rule else "default"
        source = decision_obj.source

        if decision == Decision.ALLOW:
            self._emit(PermissionEvent(
                tool=name, arguments=arguments, decision=decision,
                executed=True, source=source,
            ))
            return None

        if decision == Decision.DENY:
            self._emit(PermissionEvent(
                tool=name, arguments=arguments, decision=decision,
                executed=False, source=source,
            ))
            return f"Error: Tool '{name}' is denied by permission policy ({rule_desc})."

        # CONFIRM: ask the user when a handler exists; fail closed otherwise.
        if self.confirmation_handler is None:
            self._emit(PermissionEvent(
                tool=name, arguments=arguments, decision=Decision.DENY,
                executed=False, source=f"{source} (no confirmation handler)",
            ))
            return (
                f"Error: Tool '{name}' requires user confirmation "
                f"({rule_desc}), but no interactive user is available. "
                "Choose an alternative approach."
            )

        try:
            approved = bool(self.confirmation_handler(name, arguments, rule_desc))
        except Exception as e:  # a broken prompt must fail closed, never crash the loop
            logger.error(f"Confirmation handler failed for '{name}': {e}")
            approved = False
        self._emit(PermissionEvent(
            tool=name, arguments=arguments,
            decision=decision if approved else Decision.DENY,
            executed=approved, source=source,
        ))
        if approved:
            return None
        return f"Error: User denied the requested tool call '{name}' ({rule_desc}). Do not retry this exact call."
