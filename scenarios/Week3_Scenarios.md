# Week 3 Demo Scenarios — Observability (Task 1)

Live-demo proposal for the Friday feedback session. Each scenario runs
end-to-end on a TA machine with only secrets in `.env` (see `.env.example`).

```bash
git checkout experimental/observability-layer
cp .env.example .env   # then set INNKUBE_API_KEY + generated Langfuse secrets
docker compose pull    # skips studymate (pull_policy: build)
docker compose build studymate
docker compose up -d postgres redis clickhouse minio
# wait for healthy, then:
docker compose up -d langfuse-web langfuse-worker prometheus
# Langfuse at http://localhost:3000 ("Ready" in web logs), Prometheus at :9092
```

Sessions run two ways — pick one per demo and say which:
- **Local:** `studymate` from the venv (uses your repo `workspace/` + `traces.jsonl`).
- **Container:** `docker compose run --rm --service-ports studymate` (fully
  sandboxed: repo tree read-only, own `studymate-artifacts` + `studymate-observability`
  volumes, zero host threads — verified: 0 threads/facts, host tree byte-identical).

---

## 1. Metrics endpoint — every run is counted (E. Khalyl)

Goal: prove the `/metrics` endpoint serves live `studymate_*` series.

1. `student > /config` — observe the Metrics Enabled / Endpoint / Trace File /
   Langfuse rows (opt-in via `STUDYMATE_METRICS_ENABLED=true`).
2. Ask anything, e.g. `student > who are you?`
3. `curl http://127.0.0.1:8000/metrics | grep studymate_agent_runs_total`
   - **Pass:** `studymate_agent_runs_total{status="success"} 1.0` (or higher).
4. `curl ... | grep studymate_llm_tokens_total`
   - **Pass:** prompt/completion counters for `qwen-agentworld-35b-a3b`.

## 2. Tool + memory instrumentation by tier (E. Khalyl)

Goal: prove tool calls and memory ops land on labeled series.

1. `student > remember that I prefer cloze flashcards` → `memory_store` fires.
2. `student > what do I prefer?` → `memory_recall` fires.
3. Query Prometheus (`http://127.0.0.1:9092` → Graph):
   `sum by (tool, source) (rate(studymate_tool_calls_total[5m]))`
   - **Pass:** `memory_store`/`memory_recall` under `source="memory"`;
     filesystem tools under `source="builtin"`.
4. `sum by (op, result) (rate(studymate_memory_ops_total[5m]))`
   - **Pass:** `store_fact{created}` then `recall{keyword|semantic}`.

## 3. Prometheus scrapes + rules (E. Khalyl)

Goal: prove the server loop is closed and the 7 required panels precompute.

1. Open `http://127.0.0.1:9092/targets`
   - **Pass:** `studymate-run → up` while an interactive
     `docker compose run --rm --service-ports studymate` session is live
     (one-off containers get no service DNS, so interactive sessions scrape
     via `host.docker.internal:8000` — see `observability/prometheus.yml`).
2. Open Status → Rules
   - **Pass:** group `studymate.agent`, 8 rules (throughput, success/failure
     rates, p95 component latency, LLM tokens, tool usage, permission
     decisions, sub-agent activity).
3. Graph `studymate:agent_throughput_5m`, `studymate:component_latency_p95_5m`
   - **Pass:** queries evaluate (empty until 5m of runs — that's normal).

## 4. Per-run traces — JSONL + Langfuse (E. Khalyl)

Goal: prove one run yields one inspectable trace in both stores.

1. After any REPL turn:
   - Local session: `tail -1 observability/traces.jsonl | python -m json.tool`
   - Container session: `docker compose run --rm --entrypoint cat studymate /app/observability/traces.jsonl`
   - **Pass:** one object with `run_id`, `model`, `iterations`,
     `tokens{prompt,completion,total}`, `tools[{name,source,duration_ms,
     status}]`, `status:"success"`. The agent's tools can never reach this
     file (outside the `workspace/artifacts` write jail; container traces
     live in their own volume, never the host file).
2. Open `http://localhost:3000 → Traces`
   - **Pass:** `studymate-run:success` with the same `run_id`, model, token
     counts, and tool calls. Export is best-effort: a down Langfuse never
     fails a run (daemon thread, warning only).

## 5. Disabled-by-default safety (E. Khalyl)

Goal: prove observability is strictly opt-in and offline-safe.

1. `STUDYMATE_METRICS_ENABLED=false studymate`, ask a question, exit.
   - **Pass:** no `:8000` listener, no `traces.jsonl` writes, suite green.
2. `pytest tests/unit/test_observability.py tests/e2e/test_observability.py`
   - **Pass:** 15 passed — no-op mode, frozen series names, schema-v1
     roundtrip, mocked Langfuse paths, fast-fail on unreachable host,
     teammate contract hooks (`permission_decisions_total`,
     `subagent_runs_total`).

---

## Cross-cutting checks

- Full suite green: `pytest` (151 passed on this branch).
- No secrets committed (`.env` gitignored; `.env.example` holds placeholders).
- No deletions anywhere: `allow_delete=False`; traces are append-only.
- Metric names frozen in `src/study_agent/observability/metrics.py` — the
  permissions/sub-agents teammates emit into `observe_permission` /
  `observe_subagent` without touching client code.
- Known environment notes: compose uses the `docker.io/langfuse` mirror
  (`docker.langfuse.com` is DNS-blocked on some networks); MinIO/S3 is
  mandatory for v4 trace ingestion (verified live — removal breaks the
  worker with `ENOTFOUND minio`); `studymate` uses `pull_policy: build`
  (local image, never pulled); image pins are major-float
  (`postgres:17`, `redis:7`, `prometheus:v3`, `langfuse:4`), ClickHouse
  pinned `25.12` for server-compatibility checks.
