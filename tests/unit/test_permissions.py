"""Unit tests for the Week 3 multi-tier permission management mechanism."""

from typing import Any, Dict, List

import pytest

from study_agent.config.settings import (
    PermissionRuleConfig,
    PermissionsConfig,
)
from study_agent.security.policy import (
    Decision,
    PermissionEvent,
    PermissionPolicy,
    Rule,
    policy_from_config,
)
from study_agent.tools.base import BaseTool, ToolRegistry


class EchoTool(BaseTool):
    """Minimal deterministic tool for gate tests."""

    def __init__(self, name: str = "echo") -> None:
        self._name = name
        self.calls: List[Dict[str, Any]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "Echoes its input."

    @property
    def parameters(self) -> Dict[str, Any]:
        return {"type": "object", "properties": {"text": {"type": "string"}}}

    def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return f"echo: {kwargs.get('text', '')}"


# ---------------------------------------------------------------- policy

class TestPolicyEvaluation:
    def test_first_match_wins(self):
        policy = PermissionPolicy(rules=[
            Rule(tools=["write_file"], decision=Decision.DENY, tier="t"),
            Rule(tools=["write_file"], decision=Decision.ALLOW, tier="t2"),
        ])
        assert policy.evaluate("write_file", {}).decision == Decision.DENY

    def test_fnmatch_pattern_matching(self):
        policy = PermissionPolicy(rules=[
            Rule(tools=["obsidian_*"], decision=Decision.ALLOW),
        ])
        assert policy.evaluate("obsidian_read_note", {}).decision == Decision.ALLOW
        assert policy.evaluate("web_search", {}).decision == policy.default_decision

    def test_default_decision_when_unmatched(self):
        policy = PermissionPolicy(rules=[], default_decision=Decision.CONFIRM)
        result = policy.evaluate("anything", {})
        assert result.decision == Decision.CONFIRM
        assert result.rule is None
        assert result.source == "default"

    def test_argument_condition_match_and_miss(self):
        policy = PermissionPolicy(rules=[
            Rule(tools=["write_file"], decision=Decision.ALLOW,
                 arguments={"path": "workspace/artifacts/*"}),
        ])
        ok = policy.evaluate("write_file", {"path": "workspace/artifacts/a.md"})
        no = policy.evaluate("write_file", {"path": "workspace/data/a.md"})
        assert ok.decision == Decision.ALLOW
        assert no.decision == Decision.CONFIRM

    def test_missing_argument_field_never_matches(self):
        policy = PermissionPolicy(rules=[
            Rule(tools=["write_file"], decision=Decision.ALLOW,
                 arguments={"path": "*"}),
        ])
        assert policy.evaluate("write_file", {}).decision == Decision.CONFIRM

    def test_session_allow_overrides_deny(self):
        policy = PermissionPolicy(rules=[
            Rule(tools=["web_search"], decision=Decision.DENY),
        ])
        assert policy.evaluate("web_search", {}).decision == Decision.DENY
        policy.allow_for_session("web_search")
        assert policy.evaluate("web_search", {}).decision == Decision.ALLOW


# ------------------------------------------------------------ config map

class TestPolicyFromConfig:
    def test_disabled_returns_none(self):
        cfg = PermissionsConfig(enabled=False)
        assert policy_from_config(cfg) is None
        assert policy_from_config(None) is None

    def test_rules_translated(self):
        cfg = PermissionsConfig(rules=[
            PermissionRuleConfig(tools=["read_file"], decision="allow", tier="observe"),
        ])
        policy = policy_from_config(cfg)
        assert policy is not None
        assert policy.default_decision == Decision.CONFIRM
        assert policy.evaluate("read_file", {}).decision == Decision.ALLOW

    def test_invalid_decision_rejected(self):
        with pytest.raises(Exception):
            policy_from_config(PermissionsConfig(rules=[
                PermissionRuleConfig(tools=["x"], decision="maybe"),
            ]))


# --------------------------------------------------------------- registry

class TestRegistryGate:
    def test_no_policy_behaves_as_before(self):
        registry = ToolRegistry()
        tool = EchoTool()
        registry.register(tool)
        assert registry.execute("echo", {"text": "hi"}) == "echo: hi"
        assert len(tool.calls) == 1

    def test_allow_executes(self):
        policy = PermissionPolicy(rules=[
            Rule(tools=["echo"], decision=Decision.ALLOW),
        ])
        registry = ToolRegistry(policy=policy)
        tool = EchoTool()
        registry.register(tool)
        assert registry.execute("echo", {"text": "hi"}) == "echo: hi"
        assert len(tool.calls) == 1

    def test_denied_returns_error_string_and_skips_tool(self):
        policy = PermissionPolicy(rules=[
            Rule(tools=["echo"], decision=Decision.DENY, tier="blocked"),
        ])
        registry = ToolRegistry(policy=policy)
        tool = EchoTool()
        registry.register(tool)
        result = registry.execute("echo", {"text": "hi"})
        assert result.startswith("Error:")
        assert "denied by permission policy" in result
        assert tool.calls == []

    def test_confirm_without_handler_fails_closed(self):
        policy = PermissionPolicy(rules=[], default_decision=Decision.CONFIRM)
        registry = ToolRegistry(policy=policy)
        tool = EchoTool()
        registry.register(tool)
        result = registry.execute("echo", {"text": "hi"})
        assert result.startswith("Error:")
        assert "no interactive user" in result
        assert tool.calls == []

    def test_confirm_handler_approved_executes(self):
        policy = PermissionPolicy(rules=[], default_decision=Decision.CONFIRM)
        registry = ToolRegistry(policy=policy,
                                confirmation_handler=lambda n, a, r: True)
        tool = EchoTool()
        registry.register(tool)
        assert registry.execute("echo", {"text": "hi"}) == "echo: hi"
        assert len(tool.calls) == 1

    def test_confirm_handler_denied_returns_error(self):
        policy = PermissionPolicy(rules=[], default_decision=Decision.CONFIRM)
        registry = ToolRegistry(policy=policy,
                                confirmation_handler=lambda n, a, r: False)
        tool = EchoTool()
        registry.register(tool)
        result = registry.execute("echo", {"text": "hi"})
        assert "User denied" in result
        assert tool.calls == []

    def test_handler_exception_fails_closed(self):
        def boom(name, args, rule):
            raise RuntimeError("prompt crashed")

        policy = PermissionPolicy(rules=[], default_decision=Decision.CONFIRM)
        registry = ToolRegistry(policy=policy, confirmation_handler=boom)
        tool = EchoTool()
        registry.register(tool)
        result = registry.execute("echo", {"text": "hi"})
        assert "User denied" in result
        assert tool.calls == []


# ------------------------------------------------------------ event hooks

class TestPermissionEvents:
    def _collect(self) -> "tuple[ToolRegistry, List[PermissionEvent]]":
        events: List[PermissionEvent] = []
        policy = PermissionPolicy(
            rules=[Rule(tools=["echo"], decision=Decision.ALLOW)],
            default_decision=Decision.CONFIRM,
        )
        registry = ToolRegistry(policy=policy)
        registry.on_permission_decision.append(events.append)
        registry.register(EchoTool())
        return registry, events

    def test_allow_event_emitted(self):
        registry, events = self._collect()
        registry.execute("echo", {"text": "x"})
        assert len(events) == 1
        assert events[0].decision == Decision.ALLOW
        assert events[0].executed is True
        assert events[0].tool == "echo"

    def test_unregistered_tool_skips_permission_gate(self):
        registry, events = self._collect()
        result = registry.execute("unknown_tool", {})
        # unknown tool -> not-registered error, no permission event
        assert "not registered" in result
        assert events == []

    def test_default_confirm_no_handler_emits_deny_event(self):
        events: List[PermissionEvent] = []
        other = ToolRegistry(policy=PermissionPolicy(
            rules=[], default_decision=Decision.CONFIRM))
        other.on_permission_decision.append(events.append)
        other.register(EchoTool(name="mystery"))
        other.execute("mystery", {})
        assert len(events) == 1
        assert events[0].decision == Decision.DENY
        assert events[0].executed is False
        assert "no confirmation handler" in events[0].source

    def test_listener_errors_do_not_break_execution(self):
        registry, _ = self._collect()

        def bad_listener(event):
            raise ValueError("listener down")

        registry.on_permission_decision.append(bad_listener)
        assert registry.execute("echo", {"text": "ok"}) == "echo: ok"
