from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from study_agent.core.types import ChatMessage, LLMResponse


class BaseLLMClient(ABC):
    """Abstract interface for LLM inference providers."""

    @abstractmethod
    def chat(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[Dict[str, Any]]] = None
    ) -> LLMResponse:
        """
        Sends conversation messages to the LLM and returns the parsed response.
        
        Args:
            messages: List of ChatMessage objects representing history.
            tools: Optional list of tool JSON schemas for function calling.
            
        Returns:
            LLMResponse with content and/or tool calls.
        """
        pass
