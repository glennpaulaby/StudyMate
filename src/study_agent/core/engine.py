import logging
import time
from datetime import datetime, timezone
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
    abort_langfuse_run,
    append_trace,
    build_run_event,
    build_usage_details,
    export_langfuse_async,
    finish_langfuse_generation,
    finish_langfuse_run,
    finish_langfuse_tool,
    new_run_id,
    start_langfuse_generation,
    start_langfuse_run,
    start_langfuse_tool,
    truncate_args,
    truncate_text,
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
        tool_registry: Optional[ToolRegistry] = None,
        config: Optional[AgentConfig] = None,
        compaction: Optional[CompactionConfig] = None,
        llm_base_url: str = "",
        system_prompt: Optional[str] = None,
        tools: Optional[ToolRegistry] = None,
        max_iterations: Optional[int] = None,
        parent_run_id: Optional[str] = None,
        on_telemetry_event: Optional[Callable[[Dict[str, Any]], None]] = None,
        observability: Optional[ObservabilityConfig] = None,
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
        self.observability = observability or ObservabilityConfig()
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

    def _obs_active(self) -> bool:
        """Traces ship only when the operator enabled metrics or Langfuse."""
        obs = self.observability
        return bool(obs and (obs.metrics_enabled or obs.langfuse_enabled))

    def _llm_params(self) -> Dict[str, Any]:
        """Best-effort model parameters for Langfuse generations (client-agnostic)."""
        config = getattr(self.llm_client, "config", None)
        params: Dict[str, Any] = {}
        for key in ("temperature", "max_tokens"):
            value = getattr(config, key, None) if config is not None else None
            if value is not None:
                params[key] = value
        return params

    @staticmethod
    def _tool_names(schemas: Optional[List[Dict[str, Any]]]) -> List[str]:
        """Available tool names for Langfuse generation input (best-effort)."""
        names: List[str] = []
        for schema in schemas or []:
            if isinstance(schema, dict):
                fn = schema.get("function", {})
                name = fn.get("name") if isinstance(fn, dict) else None
                if name:
                    names.append(str(name))
        return names

    @staticmethod
    def _system_prompt(messages: Optional[List[Any]]) -> str:
        """Leading SYSTEM content (persona + injected facts/procedures).

        This is the exact instruction block handed to the model on every
        call. Empty when the context carries no usable system message.
        """
        for message in messages or []:
            role = getattr(message, "role", "")
            role = getattr(role, "value", role)
            content = getattr(message, "content", "") or ""
            if str(role or "") == "system" and isinstance(content, str) and content.strip():
                return content
        return ""

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
        turns: List[Dict[str, Any]],
        context: ContextManager,
        run_start: float,
        thread_id: str = "",
        thread_name: str = "",
        reasoning_tokens: int = 0,
        error_message: str = "",
        live_handle: Optional[Dict[str, Any]] = None,
    ) -> AgentRunResult:
        """Emits run metrics + one trace event, then builds the result.

        When a live Langfuse span is open, it is closed with the final event
        (true Latency); otherwise the post-hoc fallback export is used.
        """
        duration_s = time.perf_counter() - run_start
        observe_run(status, duration_s, iterations)
        if self._obs_active():
            try:
                messages = [m.to_dict() for m in context.get_messages()]
            except Exception as e:
                logger.warning(f"Trace message snapshot failed: {e}")
                messages = []
            event = build_run_event(
                model=model,
                status=status,
                run_id=run_id,
                thread_id=thread_id,
                thread_name=thread_name,
                iterations=iterations,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                reasoning_tokens=reasoning_tokens,
                final_response=final_response,
                messages=messages,
                turns=turns,
                tools=tool_records,
                compactions=compactions,
                duration_s=duration_s,
                error=error_message,
                model_parameters=self._llm_params(),
                available_tools=self._tool_names(
                    self.tool_registry.list_schemas() if self.tool_registry else None
                ),
            )
            obs = self.observability
            append_trace(obs.trace_file, event)
            if obs.langfuse_enabled:
                if live_handle:
                    finish_langfuse_run(live_handle, event)
                else:
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
            reasoning_tokens=reasoning_tokens,
            run_id=run_id,
            parent_run_id=self.parent_run_id,
            metadata={
                "run_id": run_id,
                "parent_run_id": self.parent_run_id,
                "iterations": iterations,
                "tool_calls_count": tool_calls_count,
            },
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
        context: Optional[ContextManager] = None,
        on_tool_start: Optional[Callable[[ToolCall], None]] = None,
        on_tool_end: Optional[Callable[[ToolCall, str], None]] = None,
        thread_id: str = "",
        thread_name: str = "",
    ) -> AgentRunResult:
        """
        Runs the ReAct loop until the agent produces a terminal textual answer
        or the max_iterations budget is exceeded. Thread provenance (for
        traces) is harness-supplied; the model never sees these arguments.
        """
        run_id = new_run_id()
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
        total_reasoning_tokens = 0
        overflow_retried = False
        self.compaction_state.compacted_this_run = False
        run_start = time.perf_counter()
        model = self.llm_client_model_name()
        tool_records: List[Dict[str, Any]] = []
        turns: List[Dict[str, Any]] = []
        compactions = 0
        max_chars = self.observability.trace_max_chars

        schemas = self.tool_registry.list_schemas() if self.tool_registry else None
        model_parameters = self._llm_params()
        tool_names = self._tool_names(schemas)
        # Cursor over context history: each generation child reports only the
        # entries added since the previous LLM call (see per-turn block).
        sent_len = 0

        # Live Langfuse root span: opened now so the UI Latency column
        # measures the real run. Root input captures the run's full starting
        # request (system prompt incl. injected facts/procedures, tool
        # schemas). None when exporting is unavailable — per-turn helpers
        # and _finish degrade to the post-hoc fallback.
        live_handle: Optional[Dict[str, Any]] = None
        if self.observability.langfuse_enabled:
            try:
                live_handle = start_langfuse_run(
                    host=self.observability.langfuse_host,
                    public_key=self.observability.langfuse_public_key,
                    secret_key=self.observability.langfuse_secret_key,
                    run_id=run_id,
                    model=model,
                    thread_id=thread_id,
                    thread_name=thread_name,
                    user_message=user_message,
                    model_parameters=model_parameters,
                    available_tools=tool_names,
                    system_prompt=self._system_prompt(context.get_messages()),
                    tool_schemas=schemas if isinstance(schemas, list) else [],
                    tool_choice="auto" if schemas else "none",
                    injected_memory=context.injected_note or "",
                    max_chars=max_chars,
                )
            except Exception as e:
                logger.debug(f"Live Langfuse span skipped: {e}")
                live_handle = None

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
            # Model-input delta: entries added since the previous call. Each
            # call's input = previous input + delta, so the generation chain
            # reconstructs the full request with zero duplication. Compaction
            # may replace history (length shrinks) — then ship the snapshot.
            try:
                history = context.get_messages()
                if len(history) < sent_len:
                    compacted = True
                    new_messages = [m.to_dict() for m in history]
                else:
                    compacted = False
                    new_messages = [m.to_dict() for m in history[sent_len:]]
                sent_len = len(history)
            except Exception as e:
                logger.debug(f"Model-input delta snapshot skipped: {e}")
                compacted = False
                new_messages = []
            gen_span = None
            if live_handle is not None:
                gen_span = start_langfuse_generation(
                    live_handle,
                    name=f"llm-turn-{iterations}",
                    turn=iterations,
                    available_tools=tool_names,
                    new_messages=new_messages,
                    compacted=compacted,
                )
            try:
                response = self.llm_client.chat(
                    messages=context.get_messages(),
                    tools=schemas if schemas else None
                )
            except Exception as e:
                llm_wall_end = datetime.now(timezone.utc)
                finish_langfuse_generation(
                    gen_span,
                    output={"error": str(e)},
                    model=model,
                    usage_details=build_usage_details(),
                    model_parameters=model_parameters,
                    completion_start_time=llm_wall_end,
                    level="ERROR",
                    status_message=str(e),
                )
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
                # Terminal LLM failure: close the live span (the REPL error
                # path only appends JSONL) before surfacing to the caller.
                abort_langfuse_run(live_handle, str(e))
                raise

            observe_llm(
                model,
                "ok",
                time.perf_counter() - llm_start,
                response.prompt_tokens,
                response.completion_tokens,
                response.reasoning_tokens,
            )
            observe_component_latency("llm", time.perf_counter() - llm_start)

            total_prompt_tokens += response.prompt_tokens
            total_completion_tokens += response.completion_tokens
            total_tokens += response.total_tokens
            total_reasoning_tokens += response.reasoning_tokens
            llm_wall_end = datetime.now(timezone.utc)
            llm_latency_s = round(time.perf_counter() - llm_start, 3)

            # Non-streaming TTFT ≈ full latency: the first token arrives with
            # the whole response, so completion start = receipt time.
            finish_langfuse_generation(
                gen_span,
                output={
                    "content": truncate_text(response.content or "", max_chars),
                    "reasoning_content": truncate_text(
                        response.reasoning_content or "", max_chars
                    ),
                    "tool_calls": [
                        {"name": tc.name, "arguments": truncate_args(tc.arguments, max_chars)}
                        for tc in response.tool_calls
                    ],
                    "finish_reason": response.finish_reason or "",
                },
                model=model,
                usage_details=build_usage_details(
                    response.prompt_tokens,
                    response.completion_tokens,
                    response.total_tokens,
                ),
                model_parameters=model_parameters,
                completion_start_time=llm_wall_end,
            )

            # Trace record for this iteration (LLM "parts"); tool observations
            # append below as they execute.
            turn: Dict[str, Any] = {
                "n": iterations,
                "llm": {
                    "content": response.content or "",
                    "reasoning_content": response.reasoning_content or "",
                    "tool_calls": [
                        {"name": tc.name, "arguments": truncate_args(tc.arguments, max_chars)}
                        for tc in response.tool_calls
                    ],
                    "finish_reason": response.finish_reason or "",
                    "prompt_tokens": response.prompt_tokens,
                    "completion_tokens": response.completion_tokens,
                    "reasoning_tokens": response.reasoning_tokens,
                    "latency_s": llm_latency_s,
                },
                "tools": [],
            }
            turns.append(turn)

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
                    res = self._finish(
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
                        turns=turns,
                        context=context,
                        run_start=run_start,
                        thread_id=thread_id,
                        thread_name=thread_name,
                        reasoning_tokens=total_reasoning_tokens,
                        error_message=message,
                        live_handle=live_handle,
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

                res = self._finish(
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
                    turns=turns,
                    context=context,
                    run_start=run_start,
                    thread_id=thread_id,
                    thread_name=thread_name,
                    reasoning_tokens=total_reasoning_tokens,
                    live_handle=live_handle,
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

                tool_start = time.perf_counter()
                registered = self.tool_registry.get(tool_call.name) if self.tool_registry else None
                tool_source_name = tool_source(registered) if registered is not None else "builtin"
                # Server label for MCP health widgets: the external bridge
                # (duck-typed via remote_name, no tools.mcp import in core)
                # vs the local vault tools vs non-MCP tools.
                remote_name = getattr(registered, "remote_name", None)
                if isinstance(remote_name, str) and remote_name:
                    tool_server = "external-mcp"
                elif tool_source_name == "mcp":
                    tool_server = "local-mcp"
                else:
                    tool_server = ""
                tool_span = None
                if live_handle is not None:
                    tool_span = start_langfuse_tool(
                        live_handle["span"],
                        name=tool_call.name,
                        input=truncate_args(tool_call.arguments, max_chars),
                        metadata={"turn": iterations, "source": tool_source_name, "server": tool_server},
                    )
                observation = self.tool_registry.execute(
                    name=tool_call.name,
                    arguments=tool_call.arguments
                )
                tool_duration_ms = (time.perf_counter() - tool_start) * 1000.0
                tool_status = "error" if observation.startswith("Error") else "ok"
                finish_langfuse_tool(
                    tool_span,
                    output=truncate_text(observation, max_chars),
                    metadata={
                        "turn": iterations,
                        "status": tool_status,
                        "duration_ms": round(tool_duration_ms, 2),
                        "source": tool_source_name,
                        "server": tool_server,
                    },
                    level="ERROR" if tool_status == "error" else "DEFAULT",
                    status_message=truncate_text(observation, 500) if tool_status == "error" else None,
                )
                tool_records.append(
                    {
                        "name": tool_call.name,
                        "source": tool_source_name,
                        "server": tool_server,
                        "duration_ms": round(tool_duration_ms, 2),
                        "status": "error" if observation.startswith("Error") else "ok",
                        "arguments": truncate_args(tool_call.arguments, max_chars),
                    }
                )
                turn["tools"].append({"name": tool_call.name, "observation": observation})

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
        res = self._finish(
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
            turns=turns,
            context=context,
            run_start=run_start,
            thread_id=thread_id,
            thread_name=thread_name,
            reasoning_tokens=total_reasoning_tokens,
            error_message=f"Reached maximum iterations ({self.config.max_iterations})",
            live_handle=live_handle,
        )
        self._emit_telemetry("run_end", {
            "run_id": run_id,
            "is_success": False,
            "iterations": iterations,
            "total_tokens": total_tokens,
            "error_message": res.error_message,
        })
        return res
