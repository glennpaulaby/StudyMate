"""E2E: ReAct loop with the Week 3 permission gate on built-in and MCP-style tools."""

from pathlib import Path
from typing import Any, Dict, List

from study_agent.config.settings import AgentConfig, FilesystemToolConfig
from study_agent.core.context import ContextManager
from study_agent.core.engine import AgentEngine
from study_agent.core.types import ToolCall
from study_agent.llm.mock import MockLLMClient
from study_agent.security.policy import Decision, PermissionEvent, PermissionPolicy, Rule
from study_agent.tools.base import BaseTool, ToolRegistry
from study_agent.tools.filesystem import register_filesystem_tools


class FakeWebSearchTool(BaseTool):
    """Stands in for the MCP web_search tool: consequential, needs confirmation."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return "Search the web (fake, for tests)."

    @property
    def parameters(self) -> Dict[str, Any]:
        return {"type": "object", "properties": {"query": {"type": "string"}}}

    def execute(self, **kwargs: Any) -> str:
        self.calls += 1
        return "search result: dynamic programming"


def _build_registry(tmp_path: Path, handler) -> "tuple[ToolRegistry, List[PermissionEvent], FakeWebSearchTool]":
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = FilesystemToolConfig(
        allowed_read_paths=[str(workspace)],
        allowed_write_paths=[str(workspace)],
    )
    policy = PermissionPolicy(
        rules=[
            Rule(tools=["read_file", "list_files", "search_text"],
                 decision=Decision.ALLOW, tier="observe"),
            Rule(tools=["write_file", "create_folder"],
                 decision=Decision.ALLOW, tier="sandbox-edit"),
            Rule(tools=["web_search"],
                 decision=Decision.CONFIRM, tier="consequential"),
        ],
        default_decision=Decision.CONFIRM,
    )
    registry = ToolRegistry(policy=policy, confirmation_handler=handler)
    events: List[PermissionEvent] = []
    registry.on_permission_decision.append(events.append)
    register_filesystem_tools(registry, base_dir=workspace, config=config)
    search = FakeWebSearchTool()
    registry.register(search)
    return registry, events, search


def test_e2e_confirmed_tool_call_executes_and_is_recorded(tmp_path: Path):
    """
    Permission E2E (approved path): the agent wants web_search (tier:
    consequential -> require-user-confirmation), the user approves, the tool
    runs, its result flows back through the ReAct loop, and every permission
    decision is emitted as an event for the future metrics layer.
    """
    asked: List[str] = []

    def handler(name: str, arguments: Dict[str, Any], rule: str) -> bool:
        asked.append(name)
        return True

    registry, events, search = _build_registry(tmp_path, handler)

    mock_llm = MockLLMClient()
    mock_llm.add_response(
        content="I will search the web first.",
        tool_calls=[ToolCall(id="c1", name="web_search", arguments={"query": "dynamic programming"})],
    )
    mock_llm.add_response(
        content="Now writing the note.",
        tool_calls=[ToolCall(id="c2", name="write_file",
                             arguments={"path": "concept.md", "content": "# DP"})],
    )
    mock_llm.add_response(content="Done: searched and wrote concept.md.")

    engine = AgentEngine(llm_client=mock_llm, tool_registry=registry,
                         config=AgentConfig(max_iterations=10))
    context = ContextManager()
    result = engine.run("Research dynamic programming and save a note.", context)

    assert asked == ["web_search"]
    assert search.calls == 1
    assert (tmp_path / "workspace" / "concept.md").is_file()
    assert result.is_success
    decisions = [(e.tool, e.decision, e.executed) for e in events]
    assert ("web_search", Decision.CONFIRM, True) in decisions
    assert ("write_file", Decision.ALLOW, True) in decisions


def test_e2e_denied_tool_recovers_with_allowed_alternative(tmp_path: Path):
    """
    Permission E2E (denied path): the user refuses web_search. The denial must
    land in the conversation as a tool observation (not an exception), the
    agent must see it and continue with an allowed tool, and the denied call
    must never reach the tool itself.
    """
    registry, events, search = _build_registry(
        tmp_path, lambda name, arguments, rule: False)

    mock_llm = MockLLMClient()
    mock_llm.add_response(
        content="I will search the web.",
        tool_calls=[ToolCall(id="c1", name="web_search", arguments={"query": "dp"})],
    )
    mock_llm.add_response(
        content="Search was denied; I will write from my own knowledge instead.",
        tool_calls=[ToolCall(id="c2", name="write_file",
                             arguments={"path": "concept.md", "content": "# DP from memory"})],
    )
    mock_llm.add_response(content="The web search was denied, so I wrote the note from memory.")

    engine = AgentEngine(llm_client=mock_llm, tool_registry=registry,
                         config=AgentConfig(max_iterations=10))
    context = ContextManager()
    result = engine.run("Research dynamic programming and save a note.", context)

    # Denied call never executed the tool.
    assert search.calls == 0
    # The denial reached the LLM as an observation on tool message c1.
    tool_messages = [m for m in context.get_messages() if m.role.value == "tool"]
    denial = next(m for m in tool_messages if m.tool_call_id == "c1")
    assert "User denied" in (denial.content or "")
    # The agent completed via the fallback path.
    assert result.is_success
    assert (tmp_path / "workspace" / "concept.md").is_file()
    denied_event = next(e for e in events if e.tool == "web_search")
    assert denied_event.decision == Decision.DENY
    assert denied_event.executed is False


def test_e2e_policy_denial_needs_no_handler(tmp_path: Path):
    """
    A rule-level deny (no confirmation) must block execution even with no
    confirmation handler attached — the pure-policy path used by sub-agents.
    """
    registry, events, search = _build_registry(tmp_path, None)
    # web_search is CONFIRM by rule; without a handler it must fail closed.
    mock_llm = MockLLMClient()
    mock_llm.add_response(
        content="Trying the web.",
        tool_calls=[ToolCall(id="c1", name="web_search", arguments={"query": "dp"})],
    )
    mock_llm.add_response(content="No interactive user; stopping the search attempt.")

    engine = AgentEngine(llm_client=mock_llm, tool_registry=registry,
                         config=AgentConfig(max_iterations=10))
    context = ContextManager()
    result = engine.run("Search the web for dp.", context)

    assert search.calls == 0
    assert result.is_success
    tool_messages = [m for m in context.get_messages() if m.role.value == "tool"]
    assert any("no interactive user" in (m.content or "") for m in tool_messages)
    assert events and events[-1].decision == Decision.DENY
