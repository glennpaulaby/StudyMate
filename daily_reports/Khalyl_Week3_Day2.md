# My contributions:
- impl: outcome as first-class trace tags (`success`/`error`/`max_iterations`/`empty` + thread name via `langfuse.trace.tags` stamp at finish) + explicit `status` in span output/metadata; Status column stays severity-only (DEFAULT unless error) by Langfuse semantics
- impl: full model-input visibility in traces — system prompt + tool schemas in root input, per-turn message deltas on generations (zero-duplication chain), end-of-run `messages[]` in root output, separate `injected_memory` field so facts survive truncation
- impl: periodic MCP health heartbeat (`observability/health.py`) fanning out to Prometheus gauges (`studymate_mcp_up`) + Langfuse `mcp-health:*` traces; `source`/`server` labels on all tool spans; opt-in via `STUDYMATE_HEALTH_ENABLED`
- impl: built 4 Langfuse dashboard widgets via the unstable API and placed them on Studymate Dashboard — "MCP servers live status" pivot, "MCP servers UP" / "MCP servers DOWN" side-by-side bars
- review: diagnosed `web_search` TaskGroup failure as transient DuckDuckGo throttle (direct + burst repros green, local venv) — no code fault
- review: got the self-hosted Langfuse Assistant working — root-caused "not enabled on this instance" to missing `LANGFUSE_IN_APP_AGENT_ENABLED` + env model config, wired compose/`.env.example`, proved InnKube strict-template 400s per model and switched Assistant to `qwen3-next-80b-a3b-instruct`, fixed worker→MCP reachability
- test: heartbeat probes/scheduler/event, tag stamping, delta cursor, record-children emission, ERROR-level on failed tools — full suite green at 193 passed

# Team:
- discussed bugs
- presented improvements