import io
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest
from rich.console import Console

from study_agent.cli import terminal
from study_agent.config.settings import AgentConfig, AppConfig, LLMConfig
from study_agent.core.types import ToolCall
from study_agent.llm.base import BaseLLMClient
from study_agent.llm.mock import MockLLMClient
from study_agent.study.templates import format_concept_note
from study_agent.tools.base import BaseTool, ToolRegistry


class _DummyTool(BaseTool):
    """Minimal concrete tool used to exercise the on_tool_start callback."""

    @property
    def name(self) -> str:
        return "dummy_tool"

    @property
    def description(self) -> str:
        return "A dummy tool for testing."

    @property
    def parameters(self) -> Dict[str, Any]:
        return {"type": "object", "properties": {}}

    def execute(self, **kwargs: Any) -> str:
        return "dummy tool executed"


@pytest.fixture
def console_buffer(monkeypatch):
    """Redirects the module-level Rich console to a plain, capturable buffer."""
    buf = io.StringIO()
    test_console = Console(
        file=buf, force_terminal=False, legacy_windows=False, no_color=True, width=120
    )
    monkeypatch.setattr(terminal, "console", test_console)
    return buf


def make_config(api_key: str = "test-api-key", **agent_overrides: Any) -> AppConfig:
    agent_kwargs: Dict[str, Any] = {
        "max_iterations": 5,
        "system_prompt_path": "__nonexistent_soul__.md",
    }
    agent_kwargs.update(agent_overrides)
    return AppConfig(
        llm=LLMConfig(model="test-model", api_key=api_key, base_url="https://mock.local/v1"),
        agent=AgentConfig(**agent_kwargs),
    )


def set_inputs(inputs: List[str]) -> None:
    terminal.console.input = MagicMock(side_effect=inputs)


def run(config: AppConfig, mock_llm: MockLLMClient, registry: ToolRegistry = None) -> None:
    terminal.run_cli(
        config=config,
        llm_client=mock_llm,
        tool_registry=registry if registry is not None else ToolRegistry(),
    )


# ---------------------------------------------------------------------------
# REPL exit / banner behavior
# ---------------------------------------------------------------------------


def test_exit_command_prints_banner_and_goodbye(console_buffer):
    set_inputs(["/exit"])
    run(make_config(), MockLLMClient())

    output = console_buffer.getvalue()
    assert "Welcome to StudyMate" in output
    assert "Goodbye and happy studying" in output


@pytest.mark.parametrize("command", ["/quit", "exit", "quit", "/EXIT"])
def test_exit_synonyms_all_terminate_loop(console_buffer, command):
    set_inputs([command])
    run(make_config(), MockLLMClient())

    assert "Goodbye and happy studying" in console_buffer.getvalue()


def test_empty_input_is_skipped_without_calling_engine(console_buffer):
    mock_llm = MockLLMClient()
    set_inputs(["", "   ", "/exit"])
    run(make_config(), mock_llm)

    assert mock_llm.call_history == []


def test_keyboard_interrupt_exits_gracefully(console_buffer):
    terminal.console.input = MagicMock(side_effect=KeyboardInterrupt())
    run(make_config(), MockLLMClient())

    assert "Session ended. Goodbye!" in console_buffer.getvalue()


def test_eof_error_exits_gracefully(console_buffer):
    terminal.console.input = MagicMock(side_effect=EOFError())
    run(make_config(), MockLLMClient())

    assert "Session ended. Goodbye!" in console_buffer.getvalue()


def test_missing_api_key_shows_warning(console_buffer):
    set_inputs(["/exit"])
    run(make_config(api_key=""), MockLLMClient())

    assert "No API key detected" in console_buffer.getvalue()


def test_present_api_key_shows_no_warning(console_buffer):
    set_inputs(["/exit"])
    run(make_config(api_key="present-key"), MockLLMClient())

    assert "No API key detected" not in console_buffer.getvalue()


# ---------------------------------------------------------------------------
# Slash commands that short-circuit the ReAct loop
# ---------------------------------------------------------------------------


def test_help_command_lists_all_commands(console_buffer):
    set_inputs(["/help", "/exit"])
    run(make_config(), MockLLMClient())

    output = console_buffer.getvalue()
    assert "StudyMate Commands" in output
    assert "/grill <topic>" in output
    assert "/ingest [path]" in output  # regression check: Rich markup must not swallow "[path]"


def test_templates_command_shows_all_three_templates(console_buffer):
    set_inputs(["/templates", "/exit"])
    run(make_config(), MockLLMClient())

    output = console_buffer.getvalue()
    assert "Obsidian Concept Note Template" in output
    assert "Obsidian Lecture Summary Template" in output
    assert "Flashcard Deck Template" in output


def test_config_command_shows_active_settings(console_buffer):
    config = make_config()
    set_inputs(["/config", "/exit"])
    run(config, MockLLMClient())

    output = console_buffer.getvalue()
    assert "Active Configuration" in output
    assert "test-model" in output


def test_clear_command_resets_history_and_confirms(console_buffer):
    mock_llm = MockLLMClient()
    set_inputs(["/clear", "/exit"])
    run(make_config(), mock_llm)

    assert "Conversation history reset" in console_buffer.getvalue()
    # /clear is handled locally and never reaches the engine.
    assert mock_llm.call_history == []


# ---------------------------------------------------------------------------
# /lint
# ---------------------------------------------------------------------------


def test_lint_command_reports_no_markdown_files(console_buffer, tmp_path: Path):
    empty_dir = tmp_path / "empty_vault"
    empty_dir.mkdir()
    set_inputs([f"/lint {empty_dir}", "/exit"])
    run(make_config(), MockLLMClient())

    assert "No Markdown files found" in console_buffer.getvalue()


def test_lint_command_uses_default_artifacts_dir_when_no_path_given(console_buffer, tmp_path: Path):
    missing_dir = tmp_path / "artifacts_missing"
    set_inputs(["/lint", "/exit"])
    run(make_config(artifacts_dir=str(missing_dir)), MockLLMClient())

    assert str(missing_dir) in console_buffer.getvalue()


def test_lint_command_reports_health_summary_for_clean_vault(console_buffer, tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    note_a = format_concept_note(
        title="Dijkstra Algorithm",
        course="Algorithms",
        one_line_summary="Shortest path algorithm.",
        core_intuition="Greedy relaxation.",
        formal_definition="Given graph G and source s...",
        connections=["bellman-ford"],
    )
    (vault / "dijkstra.md").write_text(note_a, encoding="utf-8")
    note_b = format_concept_note(
        title="Bellman-Ford Algorithm",
        course="Algorithms",
        one_line_summary="Handles negative weights.",
        core_intuition="DP edge relaxation.",
        formal_definition="Given graph G...",
        connections=["dijkstra"],
    )
    (vault / "bellman-ford.md").write_text(note_b, encoding="utf-8")

    set_inputs([f"/lint {vault}", "/exit"])
    run(make_config(), MockLLMClient())

    output = console_buffer.getvalue()
    assert "Vault Health Report" in output
    assert "0 (Clean)" in output
    assert "⚠️" not in output  # no broken-links table rendered


def test_lint_command_reports_broken_links_and_orphans(console_buffer, tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    note = format_concept_note(
        title="QuickSort",
        course="Algorithms",
        one_line_summary="Divide and conquer sort.",
        core_intuition="Partitioning around a pivot.",
        formal_definition="Recursive partitioning.",
        connections=["non-existent-note"],
    )
    (vault / "quicksort.md").write_text(note, encoding="utf-8")

    set_inputs([f"/lint {vault}", "/exit"])
    run(make_config(), MockLLMClient())

    output = console_buffer.getvalue()
    assert "Broken Wikilinks Found" in output
    assert "non-existent-note" in output
    assert "Orphan Pages" in output


# ---------------------------------------------------------------------------
# /grill
# ---------------------------------------------------------------------------


def test_grill_command_with_explicit_topic_launches_session(console_buffer):
    mock_llm = MockLLMClient()
    mock_llm.add_response(content="Define recursion precisely.")
    set_inputs(["/grill Recursion", "/exit"])
    run(make_config(), mock_llm)

    output = console_buffer.getvalue()
    assert "Launching Socratic Grilling Session on 'Recursion'" in output
    assert "Define recursion precisely." in output
    assert len(mock_llm.call_history) == 1
    sent_prompt = mock_llm.call_history[0][-1].content
    assert "grill the student on the topic: 'Recursion'" in sent_prompt


def test_grill_command_without_topic_defaults_to_core_concepts(console_buffer):
    mock_llm = MockLLMClient()
    mock_llm.add_response(content="Question one.")
    set_inputs(["/grill", "/exit"])
    run(make_config(), mock_llm)

    output = console_buffer.getvalue()
    assert "Core Concepts" in output
    sent_prompt = mock_llm.call_history[0][-1].content
    assert "topic: 'Core Concepts'" in sent_prompt


# ---------------------------------------------------------------------------
# /ingest
# ---------------------------------------------------------------------------


def test_ingest_command_with_explicit_path(console_buffer):
    mock_llm = MockLLMClient()
    mock_llm.add_response(content="Ingested successfully.")
    set_inputs(["/ingest workspace/data/lecture1", "/exit"])
    run(make_config(), mock_llm)

    output = console_buffer.getvalue()
    assert "Ingesting source material from 'workspace/data/lecture1'" in output
    sent_prompt = mock_llm.call_history[0][-1].content
    assert "Ingest the source material located at 'workspace/data/lecture1'" in sent_prompt
    assert "Ingested successfully." in output


def test_ingest_command_without_path_uses_default_data_dir(console_buffer):
    mock_llm = MockLLMClient()
    mock_llm.add_response(content="Ingested.")
    set_inputs(["/ingest", "/exit"])
    run(make_config(data_dir="./workspace/custom_data"), mock_llm)

    sent_prompt = mock_llm.call_history[0][-1].content
    assert "./workspace/custom_data" in sent_prompt


# ---------------------------------------------------------------------------
# Free-text messages routed through the ReAct engine
# ---------------------------------------------------------------------------


def test_plain_message_runs_engine_and_renders_response(console_buffer):
    mock_llm = MockLLMClient()
    mock_llm.add_response(content="Recursion is a function calling itself.")
    set_inputs(["Explain recursion", "/exit"])
    run(make_config(), mock_llm)

    assert "Recursion is a function calling itself." in console_buffer.getvalue()


def test_response_with_no_text_shows_placeholder(console_buffer):
    mock_llm = MockLLMClient()
    mock_llm.add_response(content=None)
    set_inputs(["Hi", "/exit"])
    run(make_config(), mock_llm)

    assert "[Mock Response to:" in console_buffer.getvalue()


def test_tool_call_triggers_on_tool_start_callback(console_buffer):
    registry = ToolRegistry()
    registry.register(_DummyTool())

    mock_llm = MockLLMClient()
    mock_llm.add_response(
        content=None,
        tool_calls=[ToolCall(id="call_1", name="dummy_tool", arguments={"x": 1})],
    )
    mock_llm.add_response(content="Done.")
    set_inputs(["Use the tool", "/exit"])
    run(make_config(), mock_llm, registry)

    output = console_buffer.getvalue()
    assert "Action:" in output
    assert "dummy_tool" in output
    assert "Done." in output


def test_engine_exception_is_caught_and_reported(console_buffer):
    broken_llm = MagicMock(spec=BaseLLMClient)
    broken_llm.chat.side_effect = RuntimeError("connection exploded")
    set_inputs(["Trigger failure", "/exit"])
    run(make_config(), broken_llm)

    output = console_buffer.getvalue()
    assert "Error during agent execution" in output
    assert "connection exploded" in output
    assert "Goodbye and happy studying" in output


# ---------------------------------------------------------------------------
# main() entrypoint
# ---------------------------------------------------------------------------


def test_main_invokes_run_cli(monkeypatch):
    called = MagicMock()
    monkeypatch.setattr(terminal, "run_cli", called)
    terminal.main()
    called.assert_called_once_with()


def test_main_stays_in_same_terminal_by_default(monkeypatch):
    """LAUNCH_NEW_TERMINAL unset -> no window spawn even on an interactive tty."""
    monkeypatch.delenv("LAUNCH_NEW_TERMINAL", raising=False)
    monkeypatch.delenv("STUDYMATE_SPAWNED", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    spawned = MagicMock(return_value=True)
    monkeypatch.setattr(terminal, "launch_in_new_window", spawned)
    called = MagicMock()
    monkeypatch.setattr(terminal, "run_cli", called)
    terminal.main()
    spawned.assert_not_called()
    called.assert_called_once_with()


def test_main_spawns_new_window_when_enabled(monkeypatch):
    """LAUNCH_NEW_TERMINAL=true -> dedicated window, run_cli not called here."""
    monkeypatch.setenv("LAUNCH_NEW_TERMINAL", "true")
    monkeypatch.delenv("STUDYMATE_SPAWNED", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    spawned = MagicMock(return_value=True)
    monkeypatch.setattr(terminal, "launch_in_new_window", spawned)
    called = MagicMock()
    monkeypatch.setattr(terminal, "run_cli", called)
    terminal.main()
    spawned.assert_called_once_with()
    called.assert_not_called()
