Clean slate — here's the zero-to-green sequence. Run top to bottom; every step has its expected result.
Branch `experimental/observability-layer`, PowerShell in `project/group-11`.
No raw `docker run` anywhere — `compose.yaml` drives everything.

## 0. Prerequisites

```powershell
docker info --format '{{.ServerVersion}}'   # expect 29.x; else start Docker Desktop
git status --short
Test-Path .env                              # holds INNKUBE_API_KEY + Langfuse secrets
```

Secrets ownership: **you set** values from a provider or UI (`INNKUBE_API_KEY`,
`LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`, toggles/hosts). **Generated per
machine:** `POSTGRES_PASSWORD`, `DATABASE_URL`, `CLICKHOUSE_PASSWORD`,
`REDIS_AUTH`, `MINIO_ROOT_PASSWORD` + S3 keys, `NEXTAUTH_SECRET`, `SALT`,
`ENCRYPTION_KEY`. Rule: if you asked someone (or some UI) for the value, you
set it; if it just needs to exist and match on both ends, generate it.
`.env` is gitignored — never commit it, never hand-delete generated lines
(Postgres auth breaks on the next recreate).

## 1. Pull everything + rebuild our image

```powershell
docker compose pull    # skips studymate (pull_policy: build)
docker compose build studymate
```

Expect: all pulls complete (Langfuse pair ~1 GB, ClickHouse ~1 GB).
Notes: `langfuse/*:4` come from the Docker Hub mirror because
`docker.langfuse.com` is DNS-blocked on some networks. Image pins float
within their major (`postgres:17`, `redis:7`, `prom/prometheus:v3`,
`langfuse:4`); ClickHouse is pinned to `25.12` for server-compat checks.

## 2. Boot backends, wait for healthy

```powershell
docker compose up -d postgres redis clickhouse minio
docker compose ps --format '{{.Name}} {{.Status}}'
```

Expect: all four → `healthy` (ClickHouse ~30s).

## 3. Boot app layer

```powershell
docker compose up -d langfuse-web langfuse-worker prometheus
docker compose logs langfuse-web --tail 3   # "Ready" after 2-3 min on cold boot
```

## 4. Verify endpoints

```powershell
Invoke-RestMethod http://localhost:3000/api/public/health
# {"status":"OK","version":"4.35.0"}
Invoke-RestMethod http://127.0.0.1:9092/-/healthy
(Invoke-RestMethod 'http://127.0.0.1:9092/api/v1/rules').data.groups  # 8 rules
```

## 5. Langfuse account — check first, recreate only if needed

Open **http://localhost:3000**. If your org/login is still there, volumes survived
and your `.env` keys are still valid — skip to step 6. If it shows first-run signup,
the volumes went with the wipe: sign up, create org + project, get fresh keys,
update `.env` (`LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`), and you're done —
no code changes needed.

## 6. Sessions: local vs. container

- **Local:** `studymate` from the venv — uses your repo `workspace/` and
  `observability/traces.jsonl` directly.
- **Container (sandboxed):**
  `docker compose run --rm --service-ports studymate`
  The repo tree is read-only inside; generated notes, `memory.db`, and traces
  live in container-owned volumes (`studymate-artifacts`,
  `studymate-observability`). Verified: 0 host threads visible, host tree
  byte-identical after container writes. Team-shared procedures stay visible
  read-only (approved routines still inject).
- Never run bare `up -d`: it resurrects a detached `studymate` REPL that grabs
  `:8000` and blocks interactive sessions. Scope it
  (`up -d postgres redis clickhouse minio langfuse-web langfuse-worker prometheus`).

In the REPL: `/config` → metrics + Langfuse enabled. Ask a question.

## 7. Verify a turn end-to-end (second terminal, ~25s later)

```powershell
# Prometheus scraped the session (studymate-run: interactive runs have no
# service DNS, so they scrape via host.docker.internal:8000)
(Invoke-RestMethod 'http://127.0.0.1:9092/api/v1/query?query=studymate_agent_runs_total').data.result

# Trace log: host path for local sessions...
Get-Content observability/traces.jsonl -Tail 1
# ...volume path for container sessions:
docker compose run --rm --entrypoint cat studymate /app/observability/traces.jsonl
```

Plus the run's trace in the Langfuse UI (`http://localhost:3000 → Traces`).

## 8. Failure drill

```powershell
docker compose stop langfuse-web
```

Send another REPL turn → exactly one warning line **above** a clean
`student >` prompt (`host unreachable, run unaffected`), answer itself
unaffected. Restore with `docker compose up -d langfuse-web`.

## 9. Stop / resume / nuke

```powershell
docker compose stop       # stop all, keep data (Langfuse account persists)
docker compose up -d      # resume (see §6 warning about detached studymate)
docker compose down -v    # NULEAR: deletes volumes incl. Langfuse account -> redo §5
```
## Boilerplate 
```
docker compose pull
docker compose build studymate
docker compose up -d postgres redis clickhouse minio langfuse-web langfuse-worker prometheus
docker compose run --rm --service-ports studymate
```