from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class Role(str, Enum):
    """Roles in an agent conversation turn."""
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolCall(BaseModel):
    """Represents a tool call decision made by the LLM."""
    id: str = Field(description="Unique tool call identifier")
    name: str = Field(description="Name of the function to invoke")
    arguments: Dict[str, Any] = Field(default_factory=dict, description="Parsed arguments for the function")


class ToolResult(BaseModel):
    """Represents the observation resulting from a tool execution."""
    tool_call_id: str
    name: str
    content: str
    is_error: bool = False


class ChatMessage(BaseModel):
    """Standard unified message format across all harness components."""
    role: Role
    content: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serializes to standard OpenAI-compatible message dictionary."""
        data: Dict[str, Any] = {"role": self.role.value}
        if self.content is not None:
            data["content"] = self.content
        if self.tool_calls:
            data["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": tc.arguments if isinstance(tc.arguments, str) else str(tc.arguments).replace("'", '"')
                    }
                }
                for tc in self.tool_calls
            ]
        if self.tool_call_id:
            data["tool_call_id"] = self.tool_call_id
        if self.name:
            data["name"] = self.name
        return data


class LLMResponse(BaseModel):
    """Output from the LLM inference call."""
    content: Optional[str] = None
    tool_calls: List[ToolCall] = Field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    raw_response: Optional[Dict[str, Any]] = None
