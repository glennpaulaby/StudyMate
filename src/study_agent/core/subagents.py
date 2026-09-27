"""
Subagent Registry & Specialized Child Engine Factory for StudyMate.

Defines the configuration models, system personas, and instantiation factory
for specialized child subagents (e.g. 'examiner' and 'researcher'). Child
agents run in strictly isolated context windows with bounded ReAct iterations
and fine-grained tool whitelists.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from study_agent.core.engine import AgentEngine

from study_agent.llm.base import BaseLLMClient
from study_agent.tools.base import ToolRegistry

logger = logging.getLogger(__name__)


class SubagentConfig(BaseModel):
    """
    Specification, tool whitelists, and execution bounds for a specialized child subagent.
    
    Attributes:
        name: Unique name identifier (e.g., 'examiner', 'researcher').
        description: High-level summary of capabilities for LLM delegation.
        system_prompt: Role-specific system instructions and directives.
        allowed_tools: List of tool names permitted for this subagent.
        max_iterations: Maximum ReAct loop iterations allocated for the subagent.
    """
    name: str = Field(description="Unique name identifier for the subagent")
    description: str = Field(description="Summary of the subagent's role and capabilities")
    system_prompt: str = Field(description="Role-specific system prompt persona and instructions")
    allowed_tools: List[str] = Field(
        default_factory=list,
        description="List of tool names that this subagent is permitted to execute",
    )
    max_iterations: int = Field(
        default=10,
        ge=1,
        description="Maximum ReAct reasoning iterations for the subagent",
    )


EXAMINER_SYSTEM_PROMPT = """# Role: StudyMate Socratic Examiner Subagent 🎓

You are the **Examiner** subagent for StudyMate, dedicated to active recall, Socratic grilling, and deep comprehension testing.

## Directives & Role:
1. **Socratic Grilling**: Probe the student's understanding by posing challenging, layered, and conceptual questions.
2. **Evaluate Rigorously**: Identify misconceptions, incomplete definitions, and reasoning gaps. Guide the student to correct conclusions rather than providing answers prematurely.
3. **Verify Against Sources**: You have access ONLY to read-only tools (`read_file`, `obsidian_get_backlinks`, `memory_recall`). Use them to check ground truth in lecture materials, notes, and past memory facts.
4. **Tool Restrictions**: You are strictly read-only. You CANNOT create, modify, or delete any files or artifacts.
5. **Output**: When your assessment turn finishes, provide a structured, encouraging evaluation highlighting strengths, identified gaps, and recommended review points.
"""

RESEARCHER_SYSTEM_PROMPT = """# Role: StudyMate Literature & Topic Researcher Subagent 🔬

You are the **Researcher** subagent for StudyMate, dedicated to comprehensive literature analysis, cross-source topic exploration, and conceptual synthesis.

## Directives & Role:
1. **Topic Analysis & Synthesis**: Conduct in-depth research across study topics, decomposing complex subjects into core principles, mathematical/formal definitions, key mechanisms, edge cases, and practical examples.
2. **Multi-Perspective Synthesis**: Connect related concepts, highlight nuances, and summarize academic content systematically.
3. **Tool Restrictions**: You are restricted to exploration tools (`research_topic`). You have NO file-writing or file-modifying tools.
4. **Output**: Deliver structured, clear, and comprehensive research summaries formatted in Markdown with clear section headings.
"""


class SubagentRegistry:
    """
    Manages subagent configurations and acts as a factory for instantiating
    isolated child AgentEngine instances with restricted tool sets.
    """

    def __init__(self) -> None:
        self._subagents: Dict[str, SubagentConfig] = {}

    def register(self, config: SubagentConfig) -> None:
        """Registers a subagent configuration in the registry."""
        if config.name in self._subagents:
            logger.warning(f"Overwriting existing subagent configuration: {config.name}")
        self._subagents[config.name] = config

    def get(self, name: str) -> Optional[SubagentConfig]:
        """Retrieves a subagent configuration by name."""
        return self._subagents.get(name)

    def list_subagents(self) -> List[SubagentConfig]:
        """Returns a list of all registered subagent configurations."""
        return list(self._subagents.values())

    def list_names(self) -> List[str]:
        """Returns a list of all registered subagent names."""
        return list(self._subagents.keys())

    def create_engine(
        self,
        name: str,
        llm_client: BaseLLMClient,
        base_registry: Optional[ToolRegistry] = None,
        parent_run_id: Optional[str] = None,
        on_telemetry_event: Optional[Callable[[Dict[str, Any]], None]] = None,
        **kwargs: Any,
    ) -> AgentEngine:
        """
        Instantiates an isolated child AgentEngine configured according to
        the subagent's specification, restricting tools strictly to allowed_tools.

        Args:
            name: Name of the subagent to instantiate.
            llm_client: The shared LLM inference client.
            base_registry: Parent tool registry to filter tools and inherit security policies from.
            parent_run_id: Observability run ID linking child traces to the parent turn.
            on_telemetry_event: Telemetry listener callback.

        Returns:
            A dedicated, sandboxed AgentEngine instance.
        """
        config = self.get(name)
        if not config:
            available = ", ".join(self.list_names())
            raise ValueError(f"Subagent '{name}' is not registered. Available subagents: {available}")

        # Build isolated child ToolRegistry with only allowed tools
        child_tools = ToolRegistry()
        if base_registry is not None:
            for tool_name in config.allowed_tools:
                tool = base_registry.get(tool_name)
                if tool is not None:
                    child_tools.register(tool)
            # Inherit policy and confirmation handler if present
            if base_registry.policy is not None:
                child_tools.policy = base_registry.policy
            if base_registry.confirmation_handler is not None:
                child_tools.confirmation_handler = base_registry.confirmation_handler
            # Inherit permission-decision listeners so child gating stays
            # visible to the parent's monitors (REPL batching, Prometheus).
            child_tools.on_permission_decision.extend(
                base_registry.on_permission_decision
            )

        from study_agent.core.engine import AgentEngine

        return AgentEngine(
            llm_client=llm_client,
            tools=child_tools,
            system_prompt=config.system_prompt,
            max_iterations=config.max_iterations,
            parent_run_id=parent_run_id,
            on_telemetry_event=on_telemetry_event,
            **kwargs,
        )


def get_default_subagent_registry() -> SubagentRegistry:
    """
    Factory creating a SubagentRegistry pre-populated with the two mandatory
    specialized subagents: 'examiner' and 'researcher'.
    """
    registry = SubagentRegistry()

    # 1. Examiner Subagent (Active recall, Socratic grilling, read-only tools)
    registry.register(
        SubagentConfig(
            name="examiner",
            description="Specialized subagent for Socratic grilling, active-recall quizzes, and testing student comprehension.",
            system_prompt=EXAMINER_SYSTEM_PROMPT,
            allowed_tools=["read_file", "obsidian_get_backlinks", "memory_recall"],
            max_iterations=8,
        )
    )

    # 2. Researcher Subagent (Literature synthesis, topic breakdown, exploration tools)
    registry.register(
        SubagentConfig(
            name="researcher",
            description="Specialized subagent for literature analysis, topic breakdown, and conceptual synthesis without file writing.",
            system_prompt=RESEARCHER_SYSTEM_PROMPT,
            allowed_tools=["research_topic"],
            max_iterations=12,
        )
    )

    return registry

