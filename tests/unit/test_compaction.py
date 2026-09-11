from study_agent.config.settings import CompactionConfig
from study_agent.core.compaction import (
    SUMMARY_PREFIX,
    CompactionState,
    compaction_threshold_tokens,
    estimate_tokens,
    is_context_overflow,
    maybe_compact,
    resolve_context_window,
    clear_window_cache,
)
from study_agent.core.context import ContextManager
from study_agent.core.types import ChatMessage, Role


def _cfg(**overrides):
    """CompactionConfig with small budgets so tests trigger deterministically."""
    base = dict(threshold=0.5, buffer=100, keep_recent_messages=2, summary_max_tokens=100)
    base.update(overrides)
    return CompactionConfig(**base)


def _long_context(n_turns: int = 6, width: int = 500) -> list:
    """Builds [system, user, assistant, (old tool,) ..., user, assistant, tool] history."""
    msgs = [ChatMessage(role=Role.SYSTEM, content="System")]
    for i in range(n_turns):
        msgs.append(ChatMessage(role=Role.USER, content=f"q{i} " + "x" * width))
        msgs.append(ChatMessage(role=Role.ASSISTANT, content=f"a{i} " + "y" * width))
        if i == 1:
            msgs.append(
                ChatMessage(
                    role=Role.TOOL, content="old tool dump " + "z" * width, tool_call_id="c0", name="read_file"
                )
            )
    msgs.append(
        ChatMessage(role=Role.TOOL, content="recent tool dump " + "z" * width, tool_call_id="c1", name="read_file")
    )
    return msgs


def test_estimate_counts_content_and_tools():
    """Verify the chars/4 heuristic covers content, tool calls, and schemas."""
    assert estimate_tokens([]) >= 1
    msgs = [ChatMessage(role=Role.USER, content="x" * 400)]
    assert estimate_tokens(msgs) == 100
    with_calls = [
        ChatMessage(
            role=Role.ASSISTANT,
            content="",
            tool_calls=[{"id": "c1", "name": "read_file", "arguments": {"path": "a"}}],
        )
    ]
    assert estimate_tokens(with_calls) > 1
    assert estimate_tokens(msgs, tool_schemas=[{"type": "function"}]) > estimate_tokens(msgs)


def test_resolve_window_matrix(monkeypatch):
    """Verify explicit > gateway > suffix > default resolution order."""
    clear_window_cache()
    cfg = CompactionConfig(context_windows={"model-a": 100000})
    assert resolve_context_window("model-a", cfg, discover=False) == (100000, "config")
    assert resolve_context_window("custom[1m]", cfg, discover=False) == (1_000_000, "suffix")
    assert resolve_context_window("custom[200k]", cfg, discover=False) == (200_000, "suffix")

    import httpx as _httpx

    class _Resp:
        status_code = 200

        def json(self):
            return {"data": {"max_input_tokens": 777000}}

    monkeypatch.setattr(_httpx, "get", lambda *a, **k: _Resp())
    assert resolve_context_window("unknown-model", cfg, base_url="https://x.local") == (777000, "gateway")
    clear_window_cache()

    class _Bad:
        status_code = 404

    monkeypatch.setattr(_httpx, "get", lambda *a, **k: _Bad())
    window, source = resolve_context_window("unknown-model", cfg, base_url="https://x.local")
    assert (window, source) == (cfg.default_context_window, "default")

    def _boom(*a, **k):
        raise ConnectionError("gateway down")

    monkeypatch.setattr(_httpx, "get", _boom)
    assert resolve_context_window("unknown-model", cfg, base_url="https://x.local")[0] == cfg.default_context_window
    # Cache hit path: seed the cache, then break the transport entirely.
    from study_agent.core import compaction as _comp

    _comp._window_cache["cached-model"] = (555000, "gateway")
    monkeypatch.setattr(_httpx, "get", _boom)
    assert resolve_context_window("cached-model", cfg, base_url="https://x.local") == (555000, "gateway")
    clear_window_cache()


def test_threshold_keeps_headroom():
    """Verify the fire line leaves buffer room for the summary call itself."""
    cfg = _cfg(threshold=0.8, buffer=100)
    assert compaction_threshold_tokens(1000, cfg) == 800
    assert compaction_threshold_tokens(150, cfg) == 50  # window - buffer wins when tiny


def test_maybe_compact_shape_and_prune():
    """Verify summary replaces old turns, keeps tail, blanks old tool dumps."""
    from study_agent.llm.mock import MockLLMClient

    cfg = _cfg()
    mock = MockLLMClient()
    mock.add_response(content="Goals: revise graphs. Pending: grill.")
    messages = _long_context()
    system_before = messages[0]
    old_tool = next(m for m in messages if m.tool_call_id == "c0")
    recent_tool = messages[-1]
    assert recent_tool.tool_call_id == "c1"

    compacted, before, after = maybe_compact(messages, mock, cfg, state=CompactionState())
    assert compacted is True and after < before
    assert messages[0] is system_before and messages[0].role == Role.SYSTEM
    assert sum(1 for m in messages if m.role == Role.SYSTEM) == 1
    assert messages[1].role == Role.USER and messages[1].content.startswith(SUMMARY_PREFIX)
    assert "revise graphs" in messages[1].content
    assert old_tool.content.startswith("[Older tool output cleared")
    assert recent_tool.content.startswith("recent tool dump")
    # Summarizer was called tool-less.
    assert mock.tool_history[0] is None or mock.tool_history[0] == []


def test_maybe_compact_guards():
    """Verify tiny-history, once-per-run, disabled, and empty-summary guards."""
    from study_agent.llm.mock import MockLLMClient

    cfg = _cfg()
    tiny = [ChatMessage(role=Role.SYSTEM, content="s"), ChatMessage(role=Role.USER, content="hi")]
    assert maybe_compact(tiny, MockLLMClient(), cfg, state=CompactionState())[0] is False

    messages = _long_context()
    state = CompactionState()
    mock = MockLLMClient()
    mock.add_response(content="summary one")
    mock.add_response(content="summary two")
    assert maybe_compact(messages, mock, cfg, state=state)[0] is True
    assert maybe_compact(messages, mock, cfg, state=state)[0] is False  # once per run
    assert maybe_compact(list(messages), mock, _cfg(enabled=False), force=False, state=CompactionState())[0] is False

    empty_mock = MockLLMClient()
    empty_mock.add_response(content="   ")
    bad_state = CompactionState()
    assert maybe_compact(_long_context(), empty_mock, cfg, state=bad_state)[0] is False
    assert bad_state.consecutive_failures == 1


def test_circuit_breaker_opens():
    """Verify repeated summarizer failures stop further attempts."""
    from study_agent.llm.mock import MockLLMClient

    class _Boom(MockLLMClient):
        def chat(self, messages, tools=None):
            raise RuntimeError("endpoint down")

    cfg = _cfg(max_consecutive_failures=2)
    state = CompactionState()
    assert maybe_compact(_long_context(), _Boom(), cfg, state=state)[0] is False
    assert maybe_compact(_long_context(), _Boom(), cfg, state=state)[0] is False
    assert state.consecutive_failures == 2
    # Breaker open: third attempt short-circuits without calling the LLM.
    boom = _Boom()
    assert maybe_compact(_long_context(), boom, cfg, state=state)[0] is False
    assert boom.call_history == []
    # Manual force still works through an open breaker.
    forced = MockLLMClient()
    forced.add_response(content="manual summary")
    assert maybe_compact(_long_context(), forced, cfg, force=True, state=state)[0] is True


def test_overflow_matcher():
    """Verify provider overflow errors are recognized (and others ignored)."""
    assert is_context_overflow(RuntimeError("prompt too long for context length")) is True
    assert is_context_overflow(ValueError("maximum context exceeded")) is True
    assert is_context_overflow(TimeoutError("LLM request timed out")) is False
    assert is_context_overflow(RuntimeError("auth failed")) is False


def test_engine_auto_compacts_before_call():
    """Verify the ReAct loop compacts an over-budget history before reasoning."""
    from study_agent.config.settings import AgentConfig
    from study_agent.core.engine import AgentEngine
    from study_agent.llm.mock import MockLLMClient
    from study_agent.tools.base import ToolRegistry

    cfg = _cfg(threshold=0.1)  # fire line 12.8k; history below is ~15k est.
    mock = MockLLMClient()
    mock.add_response(content="Compacted: graphs revision in progress.")
    mock.add_response(content="Final answer after compact.")
    engine = AgentEngine(
        llm_client=mock, tool_registry=ToolRegistry(), config=AgentConfig(max_iterations=5), compaction=cfg
    )
    context = ContextManager(custom_system_prompt="System")
    for msg in _long_context(n_turns=10, width=3000)[1:]:
        context.messages.append(msg)

    result = engine.run("continue", context)
    assert result.is_success is True
    assert "Final answer after compact." in result.final_response
    assert engine.compaction_state.compacted_this_run is True
    assert any(m.content.startswith(SUMMARY_PREFIX) for m in context.get_messages())


def test_engine_overflow_retry_once():
    """Verify a context-overflow error triggers compact-and-retry, then surfaces otherwise."""
    from study_agent.config.settings import AgentConfig
    from study_agent.core.engine import AgentEngine
    from study_agent.llm.mock import MockLLMClient
    from study_agent.tools.base import ToolRegistry

    class _OverflowOnce(MockLLMClient):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def chat(self, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("Hosted_vllmException: maximum context length exceeded")
            return super().chat(messages, tools)

    cfg = _cfg(threshold=0.95)  # auto-check stays quiet; retry path does the work
    mock = _OverflowOnce()
    mock.add_response(content="summary for retry")
    mock.add_response(content="recovered answer")
    engine = AgentEngine(
        llm_client=mock, tool_registry=ToolRegistry(), config=AgentConfig(max_iterations=5), compaction=cfg
    )
    context = ContextManager(custom_system_prompt="System")
    for msg in _long_context(n_turns=4)[1:]:
        context.messages.append(msg)

    result = engine.run("continue", context)
    assert result.is_success is True
    assert "recovered answer" in result.final_response

def test_usage_bar_colors_and_format():
    """Verify the live usage bar: green/yellow/red bands and human counts."""
    from study_agent.cli.terminal import _human_tokens, context_usage_bar

    assert _human_tokens(900) == "900"
    assert _human_tokens(12400) == "12.4k"
    assert _human_tokens(262144) == "262.1k"

    green = context_usage_bar(used=5000, window=262144, threshold=0.8)
    assert "[green]" in green and "(2%)" in green and "5.0k/262.1k" in green
    yellow = context_usage_bar(used=220000, window=262144, threshold=0.8)
    assert "[yellow]" in yellow and "(84%)" in yellow
    red = context_usage_bar(used=260000, window=262144, threshold=0.8)
    assert "[red]" in red
    over = context_usage_bar(used=999999, window=262144, threshold=0.8)
    assert "(100%)" in over and over.count(chr(0x2588)) == 20
    empty = context_usage_bar(used=0, window=262144, threshold=0.8)
    assert "�" not in empty and "(0%)" in empty


def test_toolbar_text_and_level():
    """Verify the sticky status text: model | thread | footprint, plus bands."""
    from study_agent.cli.terminal import create_input_session, format_toolbar_text, toolbar_level

    assert format_toolbar_text("qwen", None, 900, 262144, 0.8) == "qwen | new thread | ctx 900/262.1k (0%)"
    assert format_toolbar_text("qwen", "helo", 131072, 262144, 0.8) == "qwen | helo | ctx 131.1k/262.1k (50%)"
    assert toolbar_level(1000, 262144, 0.8) == "ok"
    assert toolbar_level(220000, 262144, 0.8) == "warn"
    assert toolbar_level(260000, 262144, 0.8) == "bad"
    # Non-TTY (pytest): no session, callers fall back to console.input.
    assert create_input_session(lambda: ("x", "ok")) is None
