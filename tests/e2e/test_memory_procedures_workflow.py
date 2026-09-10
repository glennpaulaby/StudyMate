from pathlib import Path
from study_agent.config.settings import AgentConfig
from study_agent.core.context import ContextManager
from study_agent.core.engine import AgentEngine
from study_agent.llm.mock import MockLLMClient
from study_agent.study.procedures import DRAFT, format_procedure, format_procedures_block, match_procedures
from study_agent.tools.base import ToolRegistry


def _teach(tmp_path: Path) -> Path:
    """Teaches the ingest routine once (mem0 Act 1: taught by one)."""
    path = tmp_path / "ingest-lecture.md"
    path.write_text(
        format_procedure(
            "ingest-lecture",
            "1. list files\n2. read each\n3. write notes",
            triggers=["ingest", "lecture"],
            rationale="Listing first prevents silent skips.",
            status="approved",
        ),
        encoding="utf-8",
    )
    return path


def test_e2e_taught_routine_reused_by_different_request(tmp_path: Path):
    """
    E2E (mem0 Act 2): a different request reuses the taught routine untold.
    Asserts the routine text reached the LLM payload (injected before acting).
    """
    _teach(tmp_path)

    mock = MockLLMClient()
    mock.add_response(content="Ingested following the routine.")
    engine = AgentEngine(llm_client=mock, tool_registry=ToolRegistry(), config=AgentConfig(max_iterations=5))
    context = ContextManager(custom_system_prompt="System")

    user_request = "please ingest the slides for chapter 2"  # different surface form, same job
    matched = match_procedures(tmp_path, user_request)
    assert [p.name for p in matched] == ["ingest-lecture"]
    context.inject_system_note(format_procedures_block(matched))

    result = engine.run(user_request, context)
    assert result.is_success is True
    sent_contents = " ".join((m.content or "") for m in mock.call_history[0])
    assert "ingest-lecture" in sent_contents
    assert "list files" in sent_contents


def test_e2e_draft_surfaced_never_followed(tmp_path: Path):
    """Verify draft routines match for surfacing but stay out of injection."""
    draft = tmp_path / "grill-physics.md"
    draft.write_text(
        format_procedure("grill-physics", "1. ask", triggers=["grill"], status=DRAFT),
        encoding="utf-8",
    )
    assert match_procedures(tmp_path, "grill me on physics") == []  # approved-only default
    drafts = match_procedures(tmp_path, "grill me on physics", statuses=(DRAFT,))
    assert [p.name for p in drafts] == ["grill-physics"]
    assert "grill-physics" in format_procedures_block(drafts)  # surfaced text exists...
