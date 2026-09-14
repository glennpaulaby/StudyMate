import logging
import time
from typing import Any, Callable, Dict, List, Optional
from study_agent.config.settings import AgentConfig, CompactionConfig, ObservabilityConfig
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
from study_agent.observability.metrics import (
    observe_compaction,
    observe_component_latency,
    observe_llm,
    observe_run,
)
from study_agent.observability.tracing import (
    append_trace,
    build_run_event,
    export_langfuse_async,
    new_run_id,
)
from study_agent.tools.base import ToolRegistry, tool_source

logger = logging.getLogger(__name__)


def _llm_error_status(error: Exception) -> str:
    """Maps client exceptions to LLM metric statuses (mirrors client wrapping)."""
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, ConnectionError):
        return "connection"
    if isinstance(error, RuntimeError) and "uthenticat" in str(error):
        return "auth"
    return "api_error"


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
        config: Optional[AgentConfig] = None,
        compaction: Optional[CompactionConfig] = None,
        llm_base_url: str = "",
        observability: Optional[ObservabilityConfig] = None,
    ) -> None:
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.config = config or AgentConfig()
        self.compaction = compaction or CompactionConfig()
        self.compaction_state = CompactionState()
        self.llm_base_url = llm_base_url
        self.observability = observability or ObservabilityConfig()
        self._context_window: Optional[int] = None
        self._window_source: str = "unresolved"

    def _obs_active(self) -> bool:
        """Traces ship only when the operator enabled metrics or Langfuse."""
        obs = self.observability
        return bool(obs and (obs.metrics_enabled or obs.langfuse_enabled))

    def _finish(
        self,
        *,
        run_id: str,
        model: str,
        status: str,
        final_response: str,
        iterations: int,
        tool_calls_count: int,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        compactions: int,
        tool_records: List[Dict[str, Any]],
        run_start: float,
        error_message: str = "",
    ) -> AgentRunResult:
        """Emits run metrics + one trace event, then builds the result."""
        observe_run(status, time.perf_counter() - run_start, iterations)
        if self._obs_active():
            event = build_run_event(
                model=model,
                status=status,
                run_id=run_id,
                iterations=iterations,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                tools=tool_records,
                compactions=compactions,
                error=error_message,
            )
            obs = self.observability
            append_trace(obs.trace_file, event)
            if obs.langfuse_enabled:
                export_langfuse_async(
                    event,
                    host=obs.langfuse_host,
                    public_key=obs.langfuse_public_key,
                    secret_key=obs.langfuse_secret_key,
                )
        return AgentRunResult(
            final_response=final_response,
            iterations=iterations,
            tool_calls_count=tool_calls_count,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            is_success=(status == "success"),
            error_message=error_message,
        )

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
        overflow_retried = False
        self.compaction_state.compacted_this_run = False
        run_id = new_run_id()
        run_start = time.perf_counter()
        model = self.llm_client_model_name()
        tool_records: List[Dict[str, Any]] = []
        compactions = 0

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
                        compactions += 1
                        observe_compaction("threshold")
                        logger.info(f"Auto-compacted context: {before} -> {after} tokens (est.).")

            # 1. Reason: Request completion from LLM with available tools
            llm_start = time.perf_counter()
            try:
                response = self.llm_client.chat(
                    messages=context.get_messages(),
                    tools=schemas if schemas else None
                )
            except Exception as e:
                llm_status = _llm_error_status(e)
                observe_llm(model, llm_status, time.perf_counter() - llm_start)
                observe_component_latency("llm", time.perf_counter() - llm_start)
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
                        compactions += 1
                        observe_compaction("overflow")
                        continue
                raise

            observe_llm(
                model,
                "ok",
                time.perf_counter() - llm_start,
                response.prompt_tokens,
                response.completion_tokens,
            )
            observe_component_latency("llm", time.perf_counter() - llm_start)

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
                    return self._finish(
                        run_id=run_id,
                        model=model,
                        status="empty",
                        final_response=message,
                        iterations=iterations,
                        tool_calls_count=tool_calls_count,
                        prompt_tokens=total_prompt_tokens,
                        completion_tokens=total_completion_tokens,
                        total_tokens=total_tokens,
                        compactions=compactions,
                        tool_records=tool_records,
                        run_start=run_start,
                        error_message=message,
                    )

                if response.finish_reason == "length" and iterations < self.config.max_iterations:
                    context.add_user_message(
                        "Continue from where you stopped. Finish the requested task and "
                        "provide the completion summary. Keep the response concise."
                    )
                    continue

                return self._finish(
                    run_id=run_id,
                    model=model,
                    status="success",
                    final_response=response.content or "",
                    iterations=iterations,
                    tool_calls_count=tool_calls_count,
                    prompt_tokens=total_prompt_tokens,
                    completion_tokens=total_completion_tokens,
                    total_tokens=total_tokens,
                    compactions=compactions,
                    tool_records=tool_records,
                    run_start=run_start,
                )

            # 3. Act: Execute each tool call requested by LLM
            for tool_call in response.tool_calls:
                tool_calls_count += 1
                if on_tool_start:
                    on_tool_start(tool_call)

                tool_start = time.perf_counter()
                observation = self.tool_registry.execute(
                    name=tool_call.name,
                    arguments=tool_call.arguments
                )
                tool_duration_ms = (time.perf_counter() - tool_start) * 1000.0
                registered = self.tool_registry.get(tool_call.name) if self.tool_registry else None
                tool_records.append(
                    {
                        "name": tool_call.name,
                        "source": tool_source(registered) if registered is not None else "builtin",
                        "duration_ms": round(tool_duration_ms, 2),
                        "status": "error" if observation.startswith("Error") else "ok",
                    }
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
        return self._finish(
            run_id=run_id,
            model=model,
            status="max_iterations",
            final_response="I reached the maximum number of reasoning steps without finishing. Please refine your query.",
            iterations=iterations,
            tool_calls_count=tool_calls_count,
            prompt_tokens=total_prompt_tokens,
            completion_tokens=total_completion_tokens,
            total_tokens=total_tokens,
            compactions=compactions,
            tool_records=tool_records,
            run_start=run_start,
            error_message=f"Reached maximum iterations ({self.config.max_iterations})",
        )
