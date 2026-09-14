# My contributions:
- implemented: Multi-tier permission management gate in `ToolRegistry` — every built-in and MCP tool call is checked against a policy before execution (branch `feat/permission-management`, commit `7c2e046`)
- implemented: New `src/study_agent/security/policy.py` — pure, I/O-free policy engine: ordered `Rule` list (fnmatch tool patterns + optional argument-field patterns), first match wins, three decisions (`allow` / `deny` / `require-user-confirmation`), `default_decision` fallback
- implemented: Fail-closed by design — `default_decision` is `require-user-confirmation`, so any rule-less tool (including runtime-discovered MCP tools) needs a human; CONFIRM without an interactive handler (tests, non-TTY stdin) or a crashing confirmation handler is treated as DENY
- implemented: Session-scoped "always allow" (`allow_for_session`) that overrides rules for the rest of the CLI run
- config: `PermissionsConfig` / `PermissionRuleConfig` in `settings.py` + three-tier rule set in `config/config.yaml` — Tier 1 "observe" (read_file, read_pdf, list_files, search_text, obsidian read/search, memory_recall), Tier 2 "sandbox-edit" (write_file, edit_file, create_folder, rename_file, obsidian write/append/edit, memory_store), Tier 3 "consequential" (web_search, fetch_web_content, expand_web_link → require confirmation)
- cli: Interactive confirmation prompt (`y`/`n`/`always`) as Rich panel showing tool name, matched rule, and truncated arguments; new `/permissions` command rendering the active policy table, default decision, and session allowances
- observability: `PermissionEvent` fired per evaluated call on an `on_permission_decision` hook seam in the registry (listener errors contained, never break a tool call); CLI wires a logger consumer — ready for Week 3 Prometheus metrics
- test: 20 unit tests (`tests/unit/test_permissions.py`: policy evaluation, config translation, registry gate, event emission) + 3 e2e tests (`tests/e2e/test_permission_workflow.py`: confirmed call executes and is recorded, denied call recovers via allowed alternative, pure-policy denial needs no handler)
- validation: Full suite passes — 156 tests (was 133 before this branch)
- docs: README updated — `/permissions` command, architecture diagram, Week 3 roadmap item marked done

Denials surface to the LLM as "Error: ..." observation strings (never exceptions), so the ReAct loop can choose an alternative approach instead of crashing.

# Team:
- Delivered the Week 3 "multi-tier permission system" roadmap item (`allow`, `deny`, `require-user-confirmation`) as a single ordered rule list in YAML rather than hardcoded per-tool checks, so sub-agents can later reuse the policy module with their own profiles
- The policy gate sits in `ToolRegistry.execute()`, covering both filesystem tools and the MCP tools built by Glenn and Khalyl without touching either integration
