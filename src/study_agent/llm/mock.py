from typing import Any, Dict, List, Optional
from study_agent.core.types import ChatMessage, LLMResponse, ToolCall
from study_agent.llm.base import BaseLLMClient


class MockLLMClient(BaseLLMClient):
    """
    Deterministic Mock LLM Client for offline testing.
    Can be configured with fixed responses, scripted tool calls, or echo behavior.
    """

    def __init__(self, responses: Optional[List[LLMResponse]] = None):
        self.responses: List[LLMResponse] = responses or []
        self.call_history: List[List[ChatMessage]] = []
        self.tool_history: List[Optional[List[Dict[str, Any]]]] = []
        self._current_index = 0

    def add_response(self, content: Optional[str] = None, tool_calls: Optional[List[ToolCall]] = None) -> None:
        """Enqueues a response for upcoming chat calls."""
        self.responses.append(
            LLMResponse(
                content=content,
                tool_calls=tool_calls or [],
                prompt_tokens=10,
                completion_tokens=10,
                total_tokens=20
            )
        )

    def chat(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[Dict[str, Any]]] = None
    ) -> LLMResponse:
        """Records the call and returns the next enqueued response."""
        self.call_history.append(messages.copy())
        self.tool_history.append(tools)
        
        if self._current_index < len(self.responses):
            response = self.responses[self._current_index]
            self._current_index += 1
            return response
            
        # Default fallback echo response if no scripted responses left
        last_message = messages[-1].content if messages else "No message"
        return LLMResponse(
            content=f"[Mock Response to: {last_message}]",
            tool_calls=[],
            prompt_tokens=5,
            completion_tokens=5,
            total_tokens=10
        )
