# My contributions:
- fix: diagnosed MCP UP/DOWN paradox as cumulative heartbeat counts over the window (80 ticks: obsidian 80 ok, bridge 6 ok + 74 fail) — replaced both boards with one failures-only `BAR_TIME_SERIES` tile, right edge reads as now-state
- fix: Langfuse `POST /dashboard-widgets -> HTTP 400` (only pivot tables take 2+ dimensions) — all tiles single-dimension; added repo-wide test banning multi-dim non-pivot snapshots
- impl: subagent + permission tiles — `Subagent runs over time`, `Permission decisions` pie per tool, `Permission decisions over time`; dropped the redundant snapshot bar per review
- impl: outcome-aware permission observation names (`permission:denied:<tool>`, `permission:always:<tool>` via new `SESSION_ALWAYS_TIER`) so denied and session-always calls split out on the pie
- review: diagnosed missing denied slice as stale container image (`src/` baked at build — rebuild, not a code fault), verified flush path (`terminal.py` turn-end) green
- docs: Last-30-min viewing note (guide §10), `provision-dashboard` step in fresh-machine flow (`.env.example` + guide §5), rewrote `Week3_Scenarios.md` to setup + traces/dashboard with 3 dashboard screenshots
- merge: everything into main
- test: widget wiring tests (tile filters vs emitted names, placements) — suite green

# Team:
- iterated dashboard designs on live screenshots (bars → pies → timeline)
- debugging
