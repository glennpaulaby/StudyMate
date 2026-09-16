"""Pure, I/O-free permission policy evaluation.

The policy is a single, ordered rule list evaluated against every tool call
that passes through the ToolRegistry, for built-in and MCP tools alike.
First match wins; unmatched calls fall back to ``default_decision``.

This module deliberately has no dependencies on the registry, the engine, or
the CLI so it can be unit-tested in isolation and reused by sub-agents with
their own profiles.
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Rule tier stamped on calls approved via a user's session-scoped "always".
#: The observability layer keys its ``permission:always:<tool>`` observation
#: names off this (import it; never retype the string).
SESSION_ALWAYS_TIER = "session-always"


class Decision(str, Enum):
    """The three permission outcomes required by the Week 3 spec."""

    ALLOW = "allow"
    DENY = "deny"
    CONFIRM = "require-user-confirmation"


@dataclass(frozen=True)
class Rule:
    """
    A single permission rule.

    ``tools`` holds fnmatch patterns matched against the tool name
    (e.g. "read_file", "obsidian_*"). ``arguments`` optionally holds
    field-name -> fnmatch pattern pairs; when present, every listed field
    must exist in the call arguments and its stringified value must match.
    ``tier`` is a free-form label (observe/sandbox-edit/consequential)
    used in reports and metrics.
    """

    tools: List[str]
    decision: Decision
    tier: str = ""
    arguments: Dict[str, str] = field(default_factory=dict)

    def matches(self, name: str, arguments: Dict[str, Any]) -> bool:
        """Returns True if this rule applies to the given tool call."""
        if not any(fnmatch.fnmatch(name, pattern) for pattern in self.tools):
            return False
        for key, pattern in self.arguments.items():
            if key not in arguments:
                return False
            if not fnmatch.fnmatch(str(arguments[key]), pattern):
                return False
        return True

    def describe(self) -> str:
        """Human-readable one-liner for logs and the /permissions table."""
        scope = f" [{self.tier}]" if self.tier else ""
        args = f" args={self.arguments}" if self.arguments else ""
        return f"{self.decision.value}{scope}: {self.tools}{args}"


@dataclass(frozen=True)
class PermissionDecision:
    """Result of evaluating one tool call: the decision plus the reason."""

    decision: Decision
    rule: Optional[Rule] = None

    @property
    def source(self) -> str:
        """Which part of the policy produced the decision (for metrics)."""
        if self.rule is None:
            return "default"
        return ",".join(self.rule.tools)


@dataclass(frozen=True)
class PermissionEvent:
    """Fired once per evaluated tool call; consumed by hooks/observability."""

    tool: str
    arguments: Dict[str, Any]
    decision: Decision
    executed: bool
    source: str
    run_id: str = ""
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class PermissionPolicy:
    """Ordered rule list with a default fallback decision."""

    def __init__(
        self,
        rules: Optional[List[Rule]] = None,
        default_decision: Decision = Decision.CONFIRM,
    ) -> None:
        self.rules: List[Rule] = list(rules or [])
        self.default_decision = default_decision
        # Session-scoped "always allow" entries added after a user confirms.
        self._session_allow: set[str] = set()

    def allow_for_session(self, tool_name: str) -> None:
        """Remembers a user's 'always' answer for the current session."""
        self._session_allow.add(tool_name)

    @property
    def session_allowances(self) -> List[str]:
        """Tool names the user approved with 'always' this session."""
        return sorted(self._session_allow)

    def evaluate(self, name: str, arguments: Dict[str, Any]) -> PermissionDecision:
        """Resolves the decision for one tool call. Pure; no side effects."""
        if name in self._session_allow:
            return PermissionDecision(
                decision=Decision.ALLOW, rule=Rule(tools=[name], decision=Decision.ALLOW, tier=SESSION_ALWAYS_TIER)
            )
        for rule in self.rules:
            if rule.matches(name, arguments):
                return PermissionDecision(decision=rule.decision, rule=rule)
        return PermissionDecision(decision=self.default_decision, rule=None)


def policy_from_config(config: Any) -> Optional[PermissionPolicy]:
    """
    Builds a PermissionPolicy from a PermissionsConfig (duck-typed to keep
    this module import-light). Returns None when permissions are disabled.
    """
    if config is None or not getattr(config, "enabled", False):
        return None
    rules = [
        Rule(
            tools=list(rule.tools),
            decision=Decision(rule.decision),
            tier=rule.tier,
            arguments=dict(rule.arguments),
        )
        for rule in getattr(config, "rules", [])
    ]
    default = Decision(getattr(config, "default_decision", Decision.CONFIRM.value))
    policy = PermissionPolicy(rules=rules, default_decision=default)
    logger.info(
        f"Permission policy active: {len(rules)} rules, default={default.value}."
    )
    return policy
