import logging
import uuid
from typing import Any, Callable, Dict, List, Optional
from study_agent.config.settings import AgentConfig, CompactionConfig
from study_agent.core.compaction import (
    CompactionState,
    compaction_threshold_tokens,
    estimate_tokens,
    is_context_overflow,
    maybe_compact,
    resolve_context_window,
)
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
        tool_registry: Optional[ToolRegistry] = None,
        config: Optional[AgentConfig] = None,
        compaction: Optional[CompactionConfig] = None,
        llm_base_url: str = "",
        system_prompt: Optional[str] = None,
        tools: Optional[ToolRegistry] = None,
        max_iterations: Optional[int] = None,
        parent_run_id: Optional[str] = None,
        on_telemetry_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self.llm_client = llm_client
        self.tool_registry = tools if tools is not None else (tool_registry if tool_registry is not None else ToolRegistry())
        self.config = config or AgentConfig()
        if max_iterations is not None:
            self.config = self.config.model_copy(update={"max_iterations": max_iterations})
        self.system_prompt = system_prompt
        self.parent_run_id = parent_run_id
        self.compaction = compaction or CompactionConfig()
        self.compaction_state = CompactionState()
        self.llm_base_url = llm_base_url
        self._context_window: Optional[int] = None
        self._window_source: str = "unresolved"
        self.on_telemetry_event: List[Callable[[Dict[str, Any]], None]] = []
        if on_telemetry_event is not None:
            self.on_telemetry_event.append(on_telemetry_event)

    def _emit_telemetry(self, event_type: str, data: Dict[str, Any]) -> None:
        """Emits a telemetry event dictionary to all registered listeners."""
        payload = {
            "event": event_type,
            "parent_run_id": self.parent_run_id,
            **data,
        }
        for listener in self.on_telemetry_event:
            try:
                listener(payload)
            except Exception as e:
                logger.warning(f"Telemetry listener failed: {e}")

    def _window(self) -> int:
        """Resolves (and caches) the active model's context window."""
        if self._context_window is None:
            window, source = resolve_context_window(
                self.llm_client_model_name(), self.compaction, self.llm_base_url
            )
            self._context_window = window
            self._window_source = source
            logger.info(f"Context window: {window} tokens (source: {source}).")
        return self._context_window

    def llm_client_model_name(self) -> str:
        """Best-effort model name for window resolution (client-agnostic)."""
        config = getattr(self.llm_client, "config", None)
        return getattr(config, "model", "") or ""

    def run(
        self,
        user_message: str,
        context: Optional[ContextManager] = None,
        on_tool_start: Optional[Callable[[ToolCall], None]] = None,
        on_tool_end: Optional[Callable[[ToolCall, str], None]] = None
    ) -> AgentRunResult:
        """
        Runs the ReAct loop until the agent produces a terminal textual answer
        or the max_iterations budget is exceeded.
        """
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        if context is None:
            context = ContextManager(custom_system_prompt=self.system_prompt)

        context.add_user_message(user_message)

        self._emit_telemetry("run_start", {
            "run_id": run_id,
            "user_message": user_message,
        })

        iterations = 0
        tool_calls_count = 0
        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_tokens = 0
        overflow_retried = False
        self.compaction_state.compacted_this_run = False

        schemas = self.tool_registry.list_schemas() if self.tool_registry else None

        while iterations < self.config.max_iterations:
            iterations += 1
            logger.debug(f"Starting ReAct iteration {iterations}/{self.config.max_iterations}")

            # 0. Context guard: auto-compact before the call, never after an overflow.
            if self.compaction.enabled:
                window = self._window()
                fire_line = compaction_threshold_tokens(window, self.compaction)
                if estimate_tokens(context.get_messages(), schemas) > fire_line:
                    compacted, before, after = maybe_compact(
                        context.get_messages(),
                        self.llm_client,
                        self.compaction,
                        tool_schemas=schemas,
                        state=self.compaction_state,
                    )
                    if compacted:
                        logger.info(f"Auto-compacted context: {before} -> {after} tokens (est.).")

            # 1. Reason: Request completion from LLM with available tools
            try:
                response = self.llm_client.chat(
                    messages=context.get_messages(),
                    tools=schemas if schemas else None
                )
            except Exception as e:
                if (
                    self.compaction.enabled
                    and not overflow_retried
                    and is_context_overflow(e)
                ):
                    logger.warning(f"Context overflow detected ({e}); compacting and retrying once.")
                    overflow_retried = True
                    compacted, _, _ = maybe_compact(
                        context.get_messages(),
                        self.llm_client,
                        self.compaction,
                        tool_schemas=schemas,
                        force=True,
                        state=self.compaction_state,
                    )
                    if compacted:
                        continue
                raise

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

                if not (response.content or "").strip():
                    message = (
                        "The model returned an empty response after "
                        f"{iterations} attempt(s). No completion summary was produced."
                    )
                    res = AgentRunResult(
                        final_response=message,
                        iterations=iterations,
                        tool_calls_count=tool_calls_count,
                        prompt_tokens=total_prompt_tokens,
                        completion_tokens=total_completion_tokens,
                        total_tokens=total_tokens,
                        is_success=False,
                        error_message=message,
                        run_id=run_id,
                        parent_run_id=self.parent_run_id,
                        metadata={
                            "run_id": run_id,
                            "parent_run_id": self.parent_run_id,
                            "iterations": iterations,
                            "tool_calls_count": tool_calls_count,
                        },
                    )
                    self._emit_telemetry("run_end", {
                        "run_id": run_id,
                        "is_success": False,
                        "iterations": iterations,
                        "total_tokens": total_tokens,
                        "error_message": message,
                    })
                    return res

                if response.finish_reason == "length" and iterations < self.config.max_iterations:
                    context.add_user_message(
                        "Continue from where you stopped. Finish the requested task and "
                        "provide the completion summary. Keep the response concise."
                    )
                    continue

                res = AgentRunResult(
                    final_response=response.content or "",
                    iterations=iterations,
                    tool_calls_count=tool_calls_count,
                    prompt_tokens=total_prompt_tokens,
                    completion_tokens=total_completion_tokens,
                    total_tokens=total_tokens,
                    is_success=True,
                    run_id=run_id,
                    parent_run_id=self.parent_run_id,
                    metadata={
                        "run_id": run_id,
                        "parent_run_id": self.parent_run_id,
                        "iterations": iterations,
                        "tool_calls_count": tool_calls_count,
                    },
                )
                self._emit_telemetry("run_end", {
                    "run_id": run_id,
                    "is_success": True,
                    "iterations": iterations,
                    "total_tokens": total_tokens,
                })
                return res

            # 3. Act: Execute each tool call requested by LLM
            for tool_call in response.tool_calls:
                tool_calls_count += 1
                if on_tool_start:
                    on_tool_start(tool_call)

                self._emit_telemetry("tool_start", {
                    "run_id": run_id,
                    "tool_call_id": tool_call.id,
                    "tool_name": tool_call.name,
                    "arguments": tool_call.arguments,
                })

                observation = self.tool_registry.execute(
                    name=tool_call.name,
                    arguments=tool_call.arguments
                )

                if on_tool_end:
                    on_tool_end(tool_call, observation)

                self._emit_telemetry("tool_end", {
                    "run_id": run_id,
                    "tool_call_id": tool_call.id,
                    "tool_name": tool_call.name,
                    "observation": observation,
                })

                # 4. Observe: Append execution result back to conversation context
                context.add_tool_result(
                    tool_call_id=tool_call.id,
                    name=tool_call.name,
                    content=observation
                )

        # Reached max iterations without terminal answer
        logger.warning(f"ReAct loop reached max iterations ({self.config.max_iterations})")
        res = AgentRunResult(
            final_response="I reached the maximum number of reasoning steps without finishing. Please refine your query.",
            iterations=iterations,
            tool_calls_count=tool_calls_count,
            prompt_tokens=total_prompt_tokens,
            completion_tokens=total_completion_tokens,
            total_tokens=total_tokens,
            is_success=False,
            error_message=f"Reached maximum iterations ({self.config.max_iterations})",
            run_id=run_id,
            parent_run_id=self.parent_run_id,
            metadata={
                "run_id": run_id,
                "parent_run_id": self.parent_run_id,
                "iterations": iterations,
                "tool_calls_count": tool_calls_count,
            },
        )
        self._emit_telemetry("run_end", {
            "run_id": run_id,
            "is_success": False,
            "iterations": iterations,
            "total_tokens": total_tokens,
            "error_message": res.error_message,
        })
        return res
