"""Unit tests for permission-decision trace events (Week 3 Phase 2).

Covers the ``permission:<tool>`` batched event schema widgets filter on.
"""

from study_agent.observability.events import (
    PERMISSION_THREAD_ID,
    PERMISSION_THREAD_NAME,
    build_permission_event,
)
from study_agent.security.policy import (
    SESSION_ALWAYS_TIER,
    Decision,
    PermissionEvent,
)


def _event(tool="write_file", decision=Decision.ALLOW, executed=True, source="sandbox-edit"):
    return PermissionEvent(
        tool=tool,
        arguments={"path": "a.md"},
        decision=decision,
        executed=executed,
        source=source,
    )


def test_build_permission_event_schema():
    """Verify batched records carry tool, decision, execution, and rule."""
    event = build_permission_event(
        [
            _event(),
            _event(tool="web_search", decision=Decision.DENY, executed=False, source="consequential"),
        ],
        model="m",
    )
    assert event["schema"] == 2
    assert event["thread"] == {"id": PERMISSION_THREAD_ID, "name": PERMISSION_THREAD_NAME}
    assert event["status"] == "success"
    assert event["model"] == "m"
    assert event["final_response"] == "Permissions: 1/2 gated calls executed"
    by_name = {r["name"]: r for r in event["tools"]}
    assert by_name["permission:write_file"]["status"] == "ok"
    assert by_name["permission:write_file"]["source"] == "permission"
    assert by_name["permission:write_file"]["arguments"] == {
        "decision": "allow",
        "executed": True,
        "rule": "sandbox-edit",
    }
    assert by_name["permission:denied:web_search"]["status"] == "error"
    assert by_name["permission:denied:web_search"]["arguments"]["decision"] == "deny"


def test_session_always_calls_get_their_own_names():
    """Verify 'always'-approved calls group separately for widgets."""
    event = build_permission_event(
        [_event(decision=Decision.ALLOW, executed=True, source=SESSION_ALWAYS_TIER)],
        model="m",
    )
    assert [r["name"] for r in event["tools"]] == ["permission:always:write_file"]
    assert event["tools"][0]["status"] == "ok"


def test_build_permission_event_empty_batch():
    """Verify an empty batch is still a well-formed (skippable) event."""
    event = build_permission_event([])
    assert event["status"] == "success"
    assert event["tools"] == []
    assert "0/0" in event["final_response"]


def test_registry_emissions_feed_the_batch():
    """Verify ToolRegistry permission hooks produce batchable events."""
    from study_agent.tools.base import BaseTool, ToolRegistry
    from study_agent.security.policy import PermissionPolicy, Rule

    class _Echo(BaseTool):
        @property
        def name(self):
            return "write_file"

        @property
        def description(self):
            return "echo"

        @property
        def parameters(self):
            return {"type": "object", "properties": {}}

        def execute(self, **kwargs):
            return "wrote"

    policy = PermissionPolicy(
        rules=[Rule(tools=["write_file"], decision=Decision.ALLOW, tier="sandbox-edit")],
        default_decision=Decision.DENY,
    )
    registry = ToolRegistry(policy=policy)
    registry.register(_Echo())
    seen = []
    registry.on_permission_decision.append(seen.append)

    assert registry.execute("write_file", {}) == "wrote"
    assert registry.execute("rm", {}) == "Error: Tool 'rm' is not registered."

    event = build_permission_event(seen)
    assert len(event["tools"]) == 1
    assert event["tools"][0]["name"] == "permission:write_file"
