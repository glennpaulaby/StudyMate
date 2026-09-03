import logging
from typing import Callable, Optional
from study_agent.config.settings import AgentConfig
from study_agent.core.context import ContextManager
from study_agent.core.types import AgentRunResult, ToolCall
from study_agent.llm.base import BaseLLMClient
from study_agent.tools.base import ToolRegistry

logger = logging.getLogger(__name__)


class AgentEngine:
    """
    Executes the core ReAct (Reason + Act) loop.
    Coordinates between the LLM inference client, conversation context,
    and sandboxed tool execution registry.
    """

    def __init__(
        self,
        llm_client: BaseLLMClient,
        tool_registry: ToolRegistry,
        config: Optional[AgentConfig] = None
    ) -> None:
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.config = config or AgentConfig()

    def run(
        self,
        user_message: str,
        context: ContextManager,
        on_tool_start: Optional[Callable[[ToolCall], None]] = None,
        on_tool_end: Optional[Callable[[ToolCall, str], None]] = None
    ) -> AgentRunResult:
        """
        Runs the ReAct loop until the agent produces a terminal textual answer
        or the max_iterations budget is exceeded.
        """
        context.add_user_message(user_message)

        iterations = 0
        tool_calls_count = 0
        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_tokens = 0

        schemas = self.tool_registry.list_schemas() if self.tool_registry else None

        while iterations < self.config.max_iterations:
            iterations += 1
            logger.debug(f"Starting ReAct iteration {iterations}/{self.config.max_iterations}")

            # 1. Reason: Request completion from LLM with available tools
            response = self.llm_client.chat(
                messages=context.get_messages(),
                tools=schemas if schemas else None
            )

            total_prompt_tokens += response.prompt_tokens
            total_completion_tokens += response.completion_tokens
            total_tokens += response.total_tokens

            # Append assistant's reasoning turn (content and/or tool call decisions)
            context.add_assistant_message(
                content=response.content,
                tool_calls=response.tool_calls if response.tool_calls else None
            )

            # A length cutoff can leave a final answer incomplete. Ask the model
            # to continue instead of rendering the partial response as final.
            if not response.tool_calls:
                if not (response.content or "").strip() and iterations < self.config.max_iterations:
                    context.add_user_message(
                        "Your last response was empty. Continue the requested task now. "
                        "Use the available tools if work remains, then provide a concise "
                        "completion summary."
                    )
                    continue

                if response.finish_reason == "length" and iterations < self.config.max_iterations:
                    context.add_user_message(
                        "Continue from where you stopped. Finish the requested task and "
                        "provide the completion summary. Keep the response concise."
                    )
                    continue

                return AgentRunResult(
                    final_response=response.content or "",
                    iterations=iterations,
                    tool_calls_count=tool_calls_count,
                    prompt_tokens=total_prompt_tokens,
                    completion_tokens=total_completion_tokens,
                    total_tokens=total_tokens,
                    is_success=True
                )

            # 3. Act: Execute each tool call requested by LLM
            for tool_call in response.tool_calls:
                tool_calls_count += 1
                if on_tool_start:
                    on_tool_start(tool_call)

                observation = self.tool_registry.execute(
                    name=tool_call.name,
                    arguments=tool_call.arguments
                )

                if on_tool_end:
                    on_tool_end(tool_call, observation)

                # 4. Observe: Append execution result back to conversation context
                context.add_tool_result(
                    tool_call_id=tool_call.id,
                    name=tool_call.name,
                    content=observation
                )

        # Reached max iterations without terminal answer
        logger.warning(f"ReAct loop reached max iterations ({self.config.max_iterations})")
        return AgentRunResult(
            final_response="I reached the maximum number of reasoning steps without finishing. Please refine your query.",
            iterations=iterations,
            tool_calls_count=tool_calls_count,
            prompt_tokens=total_prompt_tokens,
            completion_tokens=total_completion_tokens,
            total_tokens=total_tokens,
            is_success=False,
            error_message=f"Reached maximum iterations ({self.config.max_iterations})"
        )
