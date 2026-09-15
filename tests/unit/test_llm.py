from types import SimpleNamespace

import pytest

from study_agent.config.settings import LLMConfig
from study_agent.core.types import ChatMessage, Role, ToolCall
from study_agent.llm.client import OpenAICompatibleLLMClient
from study_agent.llm.mock import MockLLMClient


def test_mock_llm_echo_fallback():
    """Verify MockLLMClient fallback response."""
    mock = MockLLMClient()
    messages = [ChatMessage(role=Role.USER, content="Hello StudyMate")]
    
    response = mock.chat(messages)
    assert response.content == "[Mock Response to: Hello StudyMate]"
    assert len(response.tool_calls) == 0
    assert len(mock.call_history) == 1


def test_mock_llm_scripted_responses():
    """Verify MockLLMClient returns scripted responses sequentially."""
    mock = MockLLMClient()
    mock.add_response(content="First response")
    mock.add_response(
        content=None,
        tool_calls=[ToolCall(id="call_1", name="read_file", arguments={"path": "lecture1.txt"})]
    )
    
    resp1 = mock.chat([ChatMessage(role=Role.USER, content="Query 1")])
    assert resp1.content == "First response"
    assert len(resp1.tool_calls) == 0
    
    resp2 = mock.chat([ChatMessage(role=Role.USER, content="Query 2")])
    assert resp2.content is None
    assert len(resp2.tool_calls) == 1
    assert resp2.tool_calls[0].name == "read_file"
    assert resp2.tool_calls[0].arguments == {"path": "lecture1.txt"}


def _fake_completion(details):
    """Builds a minimal openai-shaped completion with the given details."""
    message = SimpleNamespace(content="hi", tool_calls=None)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    usage = SimpleNamespace(
        prompt_tokens=5, completion_tokens=12, total_tokens=17,
        completion_tokens_details=details,
    )
    return SimpleNamespace(choices=[choice], usage=usage)


def _client_with_usage(monkeypatch, details):
    client = OpenAICompatibleLLMClient(LLMConfig(api_key="test"))
    monkeypatch.setattr(
        client.client.chat.completions, "create", lambda **kw: _fake_completion(details)
    )
    return client


def test_reasoning_tokens_parsed_object_shape(monkeypatch):
    """Verify reasoning breakout parses from the object envelope."""
    client = _client_with_usage(monkeypatch, SimpleNamespace(reasoning_tokens=9))
    resp = client.chat([ChatMessage(role=Role.USER, content="hi")])
    assert resp.reasoning_tokens == 9
    assert resp.completion_tokens == 12 and resp.total_tokens == 17


def test_reasoning_tokens_parsed_dict_shape(monkeypatch):
    """Verify reasoning breakout parses from the dict envelope."""
    client = _client_with_usage(monkeypatch, {"reasoning_tokens": 4})
    resp = client.chat([ChatMessage(role=Role.USER, content="hi")])
    assert resp.reasoning_tokens == 4


@pytest.mark.parametrize("details", [None, {}, {"other": 1}, SimpleNamespace(), "junk"])
def test_reasoning_tokens_absent_means_zero(monkeypatch, details):
    """Verify missing/garbage breakouts degrade to 0, never raise."""
    client = _client_with_usage(monkeypatch, details)
    resp = client.chat([ChatMessage(role=Role.USER, content="hi")])
    assert resp.reasoning_tokens == 0


def test_reasoning_content_parsed_when_present(monkeypatch):
    """Verify chain-of-thought text is captured when the provider exposes it."""
    client = OpenAICompatibleLLMClient(LLMConfig(api_key="test"))
    message = SimpleNamespace(content="answer", tool_calls=None, reasoning_content="let me think")
    choice = SimpleNamespace(message=message, finish_reason="stop")
    usage = SimpleNamespace(
        prompt_tokens=5, completion_tokens=12, total_tokens=17,
        completion_tokens_details=None,
    )
    completion = SimpleNamespace(choices=[choice], usage=usage)
    monkeypatch.setattr(client.client.chat.completions, "create", lambda **kw: completion)
    resp = client.chat([ChatMessage(role=Role.USER, content="hi")])
    assert resp.reasoning_content == "let me think"
    assert resp.content == "answer"


def test_reasoning_content_absent_means_none(monkeypatch):
    """Verify missing thinking fields degrade to None, never raise."""
    client = _client_with_usage(monkeypatch, None)
    resp = client.chat([ChatMessage(role=Role.USER, content="hi")])
    assert resp.reasoning_content is None


def test_mock_llm_reasoning_content_passthrough():
    """Verify scripted thinking text survives the mock client."""
    mock = MockLLMClient()
    mock.add_response(content="done", reasoning_content="thinking", reasoning_tokens=3)
    resp = mock.chat([ChatMessage(role=Role.USER, content="hi")])
    assert resp.reasoning_content == "thinking"
    assert resp.reasoning_tokens == 3


def _client_with_message(monkeypatch, message):
    """Builds a client returning one scripted message envelope."""
    client = OpenAICompatibleLLMClient(LLMConfig(api_key="test"))
    choice = SimpleNamespace(message=message, finish_reason="stop")
    usage = SimpleNamespace(
        prompt_tokens=5, completion_tokens=12, total_tokens=17,
        completion_tokens_details=None,
    )
    completion = SimpleNamespace(choices=[choice], usage=usage)
    monkeypatch.setattr(client.client.chat.completions, "create", lambda **kw: completion)
    return client


def test_think_blocks_split_from_content(monkeypatch):
    """Verify inline <think> thinking splits into reasoning_content."""
    message = SimpleNamespace(
        content="<think>let me plan</think>\n\nFinal answer", tool_calls=None
    )
    resp = _client_with_message(monkeypatch, message).chat(
        [ChatMessage(role=Role.USER, content="hi")]
    )
    assert resp.content == "Final answer"
    assert resp.reasoning_content == "let me plan"


def test_reasoning_field_wins_but_content_stripped(monkeypatch):
    """Verify a parsed field beats inline tags, which are still stripped."""
    message = SimpleNamespace(
        content="<think>dup</think>Answer",
        tool_calls=None,
        reasoning_content="field-thinking",
    )
    resp = _client_with_message(monkeypatch, message).chat(
        [ChatMessage(role=Role.USER, content="hi")]
    )
    assert resp.content == "Answer"
    assert resp.reasoning_content == "field-thinking"


def test_unclosed_think_block_treated_as_reasoning(monkeypatch):
    """Verify a block cut off by max_tokens still separates its remainder."""
    message = SimpleNamespace(content="Answer so far<think>trailing thought", tool_calls=None)
    resp = _client_with_message(monkeypatch, message).chat(
        [ChatMessage(role=Role.USER, content="hi")]
    )
    assert resp.content == "Answer so far"
    assert resp.reasoning_content == "trailing thought"


def test_content_without_think_untouched(monkeypatch):
    """Verify ordinary markup never triggers the split."""
    message = SimpleNamespace(content="plain <b>html</b>", tool_calls=None)
    resp = _client_with_message(monkeypatch, message).chat(
        [ChatMessage(role=Role.USER, content="hi")]
    )
    assert resp.content == "plain <b>html</b>"
    assert resp.reasoning_content is None
