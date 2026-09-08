from pathlib import Path
from typing import List, Optional
from study_agent.core.types import ChatMessage, Role, ToolCall


DEFAULT_SYSTEM_PROMPT = (
    "You are StudyMate, an academic assistant designed to help students learn, "
    "summarize lecture notes, and generate study artifacts."
)


class ContextManager:
    """
    Manages the short-term conversation context, system prompt injection, 
    and history for the StudyMate agent.
    """

    def __init__(
        self,
        system_prompt_path: Optional[str | Path] = "config/soul.md",
        custom_system_prompt: Optional[str] = None
    ):
        self.system_prompt_path = Path(system_prompt_path) if system_prompt_path else None
        self._custom_system_prompt = custom_system_prompt
        self.messages: List[ChatMessage] = []
        self._base_system = ""
        self._injected_note: Optional[str] = None
        self._init_system_prompt()

    def _render_system_message(self) -> ChatMessage:
        """Builds the single leading SYSTEM message (base prompt + injected note)."""
        content = self._base_system
        if self._injected_note:
            content = f"{content}\n\n---\n{self._injected_note}"
        return ChatMessage(role=Role.SYSTEM, content=content)

    def _init_system_prompt(self) -> None:
        """Loads the system prompt from file or fallback default."""
        system_content = self._custom_system_prompt

        if not system_content and self.system_prompt_path and self.system_prompt_path.exists():
            try:
                system_content = self.system_prompt_path.read_text(encoding="utf-8").strip()
            except Exception:
                system_content = DEFAULT_SYSTEM_PROMPT

        if not system_content:
            system_content = DEFAULT_SYSTEM_PROMPT

        self._base_system = system_content
        self.messages = [self._render_system_message()]

    def add_user_message(self, content: str) -> ChatMessage:
        """Appends a user input turn to the context."""
        msg = ChatMessage(role=Role.USER, content=content)
        self.messages.append(msg)
        return msg

    def add_assistant_message(
        self,
        content: Optional[str] = None,
        tool_calls: Optional[List[ToolCall]] = None
    ) -> ChatMessage:
        """Appends an assistant response or tool call decision to the context."""
        msg = ChatMessage(role=Role.ASSISTANT, content=content, tool_calls=tool_calls)
        self.messages.append(msg)
        return msg

    def add_tool_result(
        self,
        tool_call_id: str,
        name: str,
        content: str
    ) -> ChatMessage:
        """Appends a tool execution observation to the context."""
        msg = ChatMessage(
            role=Role.TOOL,
            content=content,
            tool_call_id=tool_call_id,
            name=name
        )
        self.messages.append(msg)
        return msg

    def get_messages(self) -> List[ChatMessage]:
        """Returns the full conversation message history."""
        return self.messages

    def load_history(self, history: List[ChatMessage]) -> None:
        """Replaces in-memory history with persisted thread messages (keeps system prompt)."""
        system_msg = self.messages[0] if self.messages and self.messages[0].role == Role.SYSTEM else None
        if system_msg is None:
            self._init_system_prompt()
            system_msg = self.messages[0]
        self.messages = [system_msg] + list(history or [])

    def inject_system_note(self, content: str) -> Optional[ChatMessage]:
        """Merges a small high-value note (e.g. memory facts) into the leading SYSTEM message.

        A second SYSTEM entry breaks strict chat templates (vLLM/LiteLLM 400:
        "System message must be at the beginning"), so the note is folded into
        messages[0] instead of appended. Replaces any previous note.
        """
        if not (content or "").strip():
            return None
        self._injected_note = content.strip()
        if self.messages and self.messages[0].role == Role.SYSTEM:
            self.messages[0] = self._render_system_message()
        else:  # pragma: no cover - defensive; messages[0] is always SYSTEM
            self._init_system_prompt()
        return self.messages[0]

    def clear(self, keep_system_prompt: bool = True) -> None:
        """Resets conversation history."""
        if keep_system_prompt:
            self._init_system_prompt()
        else:
            self.messages = []

    @property
    def user_turn_count(self) -> int:
        """Returns number of user messages in history."""
        return sum(1 for m in self.messages if m.role == Role.USER)
