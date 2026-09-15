from pathlib import Path
from study_agent.core.context import ContextManager
from study_agent.core.types import Role


def test_context_initialization_with_default():
    """Verify ContextManager initializes with system prompt."""
    ctx = ContextManager(system_prompt_path="non_existent_soul.md")
    msgs = ctx.get_messages()
    assert len(msgs) == 1
    assert msgs[0].role == Role.SYSTEM
    assert "StudyMate" in msgs[0].content


def test_context_loading_from_soul_file(tmp_path: Path):
    """Verify loading custom persona instructions from file."""
    soul_file = tmp_path / "soul.md"
    soul_file.write_text("Custom Study Persona Prompt", encoding="utf-8")
    
    ctx = ContextManager(system_prompt_path=soul_file)
    msgs = ctx.get_messages()
    assert len(msgs) == 1
    assert msgs[0].content == "Custom Study Persona Prompt"


def test_multi_turn_history_accumulation():
    """Verify multi-turn history accumulation and clear behavior."""
    ctx = ContextManager(custom_system_prompt="System instructions")
    
    ctx.add_user_message("Explain QuickSort")
    ctx.add_assistant_message("QuickSort is a divide-and-conquer algorithm...")
    ctx.add_user_message("Give me a Python example")
    ctx.add_assistant_message("```python\ndef quicksort(): ...\n```")
    
    msgs = ctx.get_messages()
    assert len(msgs) == 5  # 1 system + 2 user + 2 assistant
    assert ctx.user_turn_count == 2
    
    # Test reset
    ctx.clear(keep_system_prompt=True)
    reset_msgs = ctx.get_messages()
    assert len(reset_msgs) == 1
    assert reset_msgs[0].role == Role.SYSTEM
    assert ctx.user_turn_count == 0


def test_injected_note_property_mirrors_merge_state():
    """Verify the read-only note view tracks inject/reset lifecycle."""
    ctx = ContextManager(custom_system_prompt="System instructions")
    assert ctx.injected_note is None
    ctx.inject_system_note("  facts block  ")
    assert ctx.injected_note == "facts block"
    assert "facts block" in ctx.get_messages()[0].content
    ctx.reset_injected_note()
    assert ctx.injected_note is None
    assert "facts block" not in ctx.get_messages()[0].content
