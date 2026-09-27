import logging
import time
from typing import Any, Callable, Dict, Optional

from study_agent.config.settings import ObservabilityConfig
from study_agent.core.subagents import SubagentRegistry, get_default_subagent_registry
from study_agent.llm.base import BaseLLMClient
from study_agent.observability.events import build_subagent_event
from study_agent.observability.metrics import observe_subagent
from study_agent.tools.base import BaseTool, ToolRegistry

logger = logging.getLogger(__name__)


class DelegateTaskTool(BaseTool):
    """
    Delegates a specialized sub-task to a registered child subagent
    (e.g., 'examiner', 'researcher') with isolated context and restricted tool privileges.
    """

    def __init__(
        self,
        llm_client: BaseLLMClient,
        subagent_registry: Optional[SubagentRegistry] = None,
        parent_tool_registry: Optional[ToolRegistry] = None,
        parent_run_id: Optional[str] = None,
        observability: Optional[ObservabilityConfig] = None,
        on_subagent_run: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self.llm_client = llm_client
        self.subagent_registry = subagent_registry or get_default_subagent_registry()
        self.parent_tool_registry = parent_tool_registry
        self.parent_run_id = parent_run_id
        self.observability = observability or ObservabilityConfig()
        self.on_subagent_run = on_subagent_run

    @property
    def name(self) -> str:
        return "delegate_task"

    @property
    def description(self) -> str:
        names = self.subagent_registry.list_names()
        names_str = ", ".join(names) if names else "examiner, researcher"
        return (
            f"Delegates a specialized academic sub-task to a child subagent ({names_str}). "
            "ONLY invoke this tool when the user explicitly requests subagent delegation or asks for a "
            "specialized child agent (e.g. 'researcher', 'examiner'). "
            "Do not invoke this tool for general questions unless the user explicitly requests subagent delegation."
        )

    @property
    def parameters(self) -> Dict[str, Any]:
        names = self.subagent_registry.list_names()
        schema: Dict[str, Any] = {
            "type": "object",
            "properties": {
                "subagent_name": {
                    "type": "string",
                    "description": f"The specialized subagent to invoke ({', '.join(names)}).",
                },
                "task": {
                    "type": "string",
                    "description": "Detailed prompt describing the sub-task for the subagent.",
                },
            },
            "required": ["subagent_name", "task"],
        }
        if names:
            schema["properties"]["subagent_name"]["enum"] = names
        return schema

    def execute(self, subagent_name: str = "", task: str = "", **kwargs: Any) -> str:
        """
        Spawns child AgentEngine instance via SubagentRegistry, runs child.run(task),
        and returns the child's final output string as the tool observation.
        Catches child execution timeouts and errors gracefully.
        """
        if not subagent_name:
            return "Error: 'subagent_name' parameter is required."

        if not task or not task.strip():
            return "Error: 'task' parameter cannot be empty."

        config = self.subagent_registry.get(subagent_name)
        if not config:
            available = ", ".join(self.subagent_registry.list_names())
            return (
                f"Error: Subagent '{subagent_name}' is not registered. "
                f"Available subagents: {available}"
            )

        try:
            logger.info(f"Delegating task to subagent '{subagent_name}': {task[:100]}...")
            child_engine = self.subagent_registry.create_engine(
                name=subagent_name,
                llm_client=self.llm_client,
                base_registry=self.parent_tool_registry,
                parent_run_id=self.parent_run_id,
                observability=self.observability,
            )
            start = time.perf_counter()
            child_result = child_engine.run(task)
            duration_s = time.perf_counter() - start
            status = "success" if child_result.is_success else "error"
            self._record_run(
                subagent_name=subagent_name,
                status=status,
                child_result=child_result,
                duration_s=duration_s,
                error="" if child_result.is_success else (
                    child_result.error_message or child_result.final_response or "unknown error"
                ),
            )

            if child_result.is_success:
                return child_result.final_response
            else:
                error_msg = child_result.error_message or child_result.final_response
                return f"Subagent '{subagent_name}' failed: {error_msg}"
        except TimeoutError as te:
            logger.error(f"Timeout in child subagent '{subagent_name}': {te}")
            self._record_run(
                subagent_name=subagent_name,
                status="error",
                child_result=None,
                duration_s=0.0,
                error=f"timed out: {te}",
            )
            return f"Error: Subagent '{subagent_name}' timed out: {str(te)}"
        except Exception as e:
            logger.error(f"Error executing subagent '{subagent_name}': {e}", exc_info=True)
            self._record_run(
                subagent_name=subagent_name,
                status="error",
                child_result=None,
                duration_s=0.0,
                error=str(e),
            )
            return f"Error executing subagent '{subagent_name}': {str(e)}"

    def _record_run(
        self,
        *,
        subagent_name: str,
        status: str,
        child_result: Any,
        duration_s: float,
        error: str,
    ) -> None:
        """Fans a finished delegation out to Prometheus + trace sinks.

        Telemetry must never break a delegation: listener errors are
        contained, mirroring ``ToolRegistry._emit``.
        """
        try:
            observe_subagent(subagent_name, status)
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(f"Subagent metric skipped: {e}")
        if self.on_subagent_run is None:
            return
        try:
            event = build_subagent_event(
                subagent_name=subagent_name,
                status=status,
                parent_run_id=self.parent_run_id or "",
                child_run_id=getattr(child_result, "run_id", "") or "",
                iterations=getattr(child_result, "iterations", 0) or 0,
                prompt_tokens=getattr(child_result, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(child_result, "completion_tokens", 0) or 0,
                reasoning_tokens=getattr(child_result, "reasoning_tokens", 0) or 0,
                duration_s=duration_s,
                summary=getattr(child_result, "final_response", "") or "",
                error=error,
            )
            self.on_subagent_run(event)
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(f"Subagent trace skipped: {e}")


class ResearchTopicTool(BaseTool):
    """
    Simulated or academic literature exploration tool for the researcher subagent.
    """

    def __init__(self, notes_dir: Optional[str] = None) -> None:
        self.notes_dir = notes_dir

    @property
    def name(self) -> str:
        return "research_topic"

    @property
    def description(self) -> str:
        return "Researches an academic topic or concept and returns structured background information and insights."

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "The academic topic, concept, or query to research.",
                },
                "depth": {
                    "type": "string",
                    "enum": ["overview", "detailed", "comparative"],
                    "description": "Depth of analysis requested (overview, detailed, comparative).",
                },
            },
            "required": ["topic"],
        }

    def execute(self, topic: str, depth: str = "detailed", **kwargs: Any) -> str:
        if not topic or not topic.strip():
            return "Error: Topic cannot be empty."
        return f"Research results for '{topic}' (depth: {depth}): Literature breakdown, core properties, and conceptual connections retrieved."


def register_delegation_tools(
    registry: ToolRegistry,
    llm_client: BaseLLMClient,
    subagent_registry: Optional[SubagentRegistry] = None,
    observability: Optional[ObservabilityConfig] = None,
    on_subagent_run: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> None:
    """
    Registers the DelegateTaskTool and ResearchTopicTool into the given ToolRegistry.
    """
    tool = DelegateTaskTool(
        llm_client=llm_client,
        subagent_registry=subagent_registry,
        parent_tool_registry=registry,
        observability=observability,
        on_subagent_run=on_subagent_run,
    )
    registry.register(tool)
    registry.register(ResearchTopicTool())

