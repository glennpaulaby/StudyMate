from study_agent.core.types import ChatMessage, Role, ToolCall
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
