# My contributions:
- refine: dedicated terminal window on `studymate` launch is now opt-in via `LAUNCH_NEW_TERMINAL` (default stays in the same terminal; gate + unit tests in `cli/terminal`)
- fix: fully sandboxed compose volumes — repo tree mounted read-only, all container writes (artifacts, memory.db, traces) redirected to container-owned named volumes, team procedures visible read-only; container sessions can no longer touch host dev data
- impl: live Langfuse span lifecycle for true run latency (root agent span opened at run start, closed in `_finish`, aborted as error on LLM failure; only flush goes to background thread)
- impl: per-turn generation children (model, usage_details input/output/total, model_parameters, completion_start_time) + per-tool children (args, observation, status, duration); session/tags/trace-name propagation via OTel context
- impl: enriched post-hoc `export_langfuse` fallback with the same children; removed double-export from terminal error path (engine's live span already covers it)
- impl: thinking capture in `llm/client` — `reasoning_content` field plus inline `<think>...</think>` split (Qwen-AgentWorld thinks by default; covers parser-on and parser-off servers, incl. unclosed blocks cut by max_tokens); thinking no longer pollutes answers, history, or context window
- test: 15 new tests (usage mapping, export children, live open/update/end, abort, engine live integration, think-block parsing incl. field-precedence + unclosed + markup passthrough) — full suite green at 178 passed
- review: mapped all 28 Langfuse trace-table columns to SDK fields (Latency was 0.00s by construction, Cost needs project price config); verified model card (thinking default, `--reasoning-parser qwen3`) and validated call shapes offline against langfuse 4.15.2

# Team:
- Divide week 3 work
- Share instructions, feedback and commands with the team to test subagent delegation locally 