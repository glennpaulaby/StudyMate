# Week 3 Demo — Traces & Dashboard (E. Khalyl)

Full setup sequence: `docs/observabilityGuide.md`. Short form:

```bash
git clone <repo> && cd project/group-11
cp .env.example .env   # set INNKUBE_API_KEY + generated secrets
docker compose pull && docker compose build studymate
docker compose up -d postgres redis clickhouse minio   # wait healthy
docker compose up -d langfuse-web langfuse-worker prometheus
# sign up at http://localhost:3000 → org + project → keys into .env, then:
docker compose up provision-dashboard
docker compose run --rm --service-ports studymate      # talk to the agent
```

Enable with `STUDYMATE_METRICS_ENABLED=true`, `STUDYMATE_HEALTH_ENABLED=true`,
`LANGFUSE_ENABLED=true` in `.env`.

## See the traces

Open `http://localhost:3000 → Traces`. Every REPL turn lands here, next to
periodic `mcp-health` heartbeat traces.

## See the dashboard

Open `http://localhost:3000 → Dashboards → Studymate Dashboard`, tile
`MCP servers status over time`, range Last 30 min. Empty right edge = all
servers UP, bars at the edge = DOWN.

What it looks like with data:

![Studymate Dashboard with data — tool calls, distributions, latency](docs/observability/dashboard-1.png)

![Studymate Dashboard with data — P95 latency and MCP server status timeline](docs/observability/dashboard-2.png)

![Permission decisions pie plus subagent and permission timelines, including a blocked call](docs/observability/dashboard-3.png)
