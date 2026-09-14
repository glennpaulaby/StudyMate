import logging
from typing import Any, Dict, Optional

from study_agent.core.subagents import SubagentRegistry, get_default_subagent_registry
from study_agent.llm.base import BaseLLMClient
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
    ) -> None:
        self.llm_client = llm_client
        self.subagent_registry = subagent_registry or get_default_subagent_registry()
        self.parent_tool_registry = parent_tool_registry
        self.parent_run_id = parent_run_id

    @property
    def name(self) -> str:
        return "delegate_task"

    @property
    def description(self) -> str:
        names = self.subagent_registry.list_names()
        names_str = ", ".join(names) if names else "examiner, researcher"
        return (
            f"Delegates a specialized academic sub-task to a child subagent ({names_str}). "
            "Spawns an isolated subagent engine and returns its final assessment or synthesis."
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

    def execute(self, subagent_name: str, task: str, **kwargs: Any) -> str:
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
            )
            child_result = child_engine.run(task)

            if child_result.is_success:
                return child_result.final_response
            else:
                error_msg = child_result.error_message or child_result.final_response
                return f"Subagent '{subagent_name}' failed: {error_msg}"
        except TimeoutError as te:
            logger.error(f"Timeout in child subagent '{subagent_name}': {te}")
            return f"Error: Subagent '{subagent_name}' timed out: {str(te)}"
        except Exception as e:
            logger.error(f"Error executing subagent '{subagent_name}': {e}", exc_info=True)
            return f"Error executing subagent '{subagent_name}': {str(e)}"


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
) -> None:
    """
    Registers the DelegateTaskTool into the given ToolRegistry.
    """
    tool = DelegateTaskTool(
        llm_client=llm_client,
        subagent_registry=subagent_registry,
        parent_tool_registry=registry,
    )
    registry.register(tool)
