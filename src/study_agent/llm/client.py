import json
import logging
from typing import Any, Dict, List, Optional
from openai import OpenAI, OpenAIError, AuthenticationError, APITimeoutError, APIConnectionError
from study_agent.config.settings import LLMConfig
from study_agent.core.types import ChatMessage, LLMResponse, ToolCall
from study_agent.llm.base import BaseLLMClient

logger = logging.getLogger(__name__)


def _reasoning_tokens(usage: Any) -> int:
    """Extracts reasoning tokens from a usage envelope (object or dict shape).

    Returns 0 when the provider omits the breakout (most non-reasoning calls).
    """
    details = getattr(usage, "completion_tokens_details", None)
    if details is None and isinstance(usage, dict):
        details = usage.get("completion_tokens_details")
    if details is None:
        return 0
    if isinstance(details, dict):
        value = details.get("reasoning_tokens", 0)
    else:
        value = getattr(details, "reasoning_tokens", 0)
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


class OpenAICompatibleLLMClient(BaseLLMClient):
    """
    LLM Client for OpenAI-compatible endpoints (such as the InnKube cluster).
    """

    def __init__(self, config: LLMConfig):
        self.config = config
        
        # InnKube / OpenAI client initialization
        # Default placeholder key if not provided (allows mock initialization)
        api_key = config.api_key or "placeholder_key"
        self.client = OpenAI(
            api_key=api_key,
            base_url=config.base_url,
            timeout=config.timeout
        )

    def chat(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[Dict[str, Any]]] = None
    ) -> LLMResponse:
        """
        Executes a chat completion call.
        """
        payload_messages = [m.to_dict() for m in messages]
        
        kwargs: Dict[str, Any] = {
            "model": self.config.model,
            "messages": payload_messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
            
        try:
            response = self.client.chat.completions.create(**kwargs)
            choice = response.choices[0]
            message = choice.message
            
            # Parse tool calls if returned by model
            parsed_tool_calls: List[ToolCall] = []
            if message.tool_calls:
                for raw_tc in message.tool_calls:
                    try:
                        args = json.loads(raw_tc.function.arguments) if isinstance(raw_tc.function.arguments, str) else raw_tc.function.arguments
                    except json.JSONDecodeError:
                        args = {"raw_arguments": raw_tc.function.arguments}
                    
                    parsed_tool_calls.append(
                        ToolCall(
                            id=raw_tc.id,
                            name=raw_tc.function.name,
                            arguments=args or {}
                        )
                    )
            
            # Token usage (reasoning is a breakout of completion per the
            # OpenAI usage envelope; 0 when the provider doesn't report it).
            usage = response.usage
            prompt_tokens = usage.prompt_tokens if usage else 0
            completion_tokens = usage.completion_tokens if usage else 0
            total_tokens = usage.total_tokens if usage else 0
            reasoning_tokens = _reasoning_tokens(usage) if usage else 0

            return LLMResponse(
                content=message.content,
                tool_calls=parsed_tool_calls,
                finish_reason=choice.finish_reason,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                reasoning_tokens=reasoning_tokens,
            )
            
        except AuthenticationError as e:
            logger.error("Authentication failed: Invalid INNKUBE_API_KEY")
            raise RuntimeError(
                "Authentication error: Failed to authenticate with LLM endpoint. "
                "Please ensure INNKUBE_API_KEY is correctly set in your .env file."
            ) from e
            
        except APITimeoutError as e:
            logger.error(f"Inference request timed out after {self.config.timeout}s")
            raise TimeoutError(f"LLM request timed out after {self.config.timeout} seconds.") from e
            
        except APIConnectionError as e:
            logger.error(f"Failed to connect to LLM endpoint at {self.config.base_url}")
            raise ConnectionError(f"Failed to connect to LLM endpoint: {self.config.base_url}. Check network connection.") from e
            
        except OpenAIError as e:
            logger.error(f"OpenAI API Error: {str(e)}")
            raise RuntimeError(f"LLM API Error: {str(e)}") from e
