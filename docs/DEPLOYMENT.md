# Agent-X — Deployment (Phase 8, provider-neutral)

**Nothing has been deployed.** No hosting provider is chosen, no cloud resource exists, and execution stays disabled.
A successful build is not production readiness: see §9 for what remains.

## 1. Hosting decision (owner)

Any host that runs two containers and reaches PostgreSQL 18 works. The options, for your decision:

| Option | Fits when | Notes |
|---|---|---|
| Single VM (any provider or on-premises) with Docker Compose, PostgreSQL on the same host (`bundled-db`) or separate | one user or a small group; simplest | You operate OS patches, backups (§6) and TLS. Cheapest |
| VM or container service + managed PostgreSQL | you want point-in-time recovery and managed patching for the database | Keep `pg_dump` backups too (logical, portable). Ensure the managed server is PostgreSQL 18 |
| Container platform (e.g. a managed container service) | several instances or zero-downtime upgrades | Needs a shared-state rate limiter at the edge. The agent worker must remain a single scheduled one-shot job (advisory locks already prevent duplicates) |

Kubernetes and microservices are not needed for this workload.

## 2. Resource requirements (estimates; memory and CPU were not load-tested)

| Component | Minimum | Notes |
|---|---|---|
| API container | 1 vCPU, 1 GB RAM | a backtest run holds its bars in memory (about 60 days × 10 instruments of 5-minute bars); leave headroom and measure under real use |
| Web container (nginx) | 0.25 vCPU, 64 MB | static files and proxy |
| PostgreSQL 18 | 1 vCPU, 2 GB RAM, 20 GB SSD | the archive grows by 750 canonical 5-minute candles per session (10 instruments × 75 bars); the full development backup is 2.1 MB |
| Backups | 3× the database size, off-host copy | see §6 |

## 3. Topology

```
Internet ──TLS──> reverse proxy / load balancer (provider's or Caddy/nginx/Traefik on the host)
                      │  HTTP, X-Forwarded-For/-Proto, Host
                      ▼
                 web (nginx-unprivileged :8080)  ── static UI, CSP, rate limits, body cap
                      │  /api, /health
                      ▼
                 api (uvicorn :8000, non-root, read-only FS)  ──>  PostgreSQL 18 (agentx role, not superuser)
   scheduled one-shots (operator-registered, optional): ingestion `python -m app.ops.cli daily`,
   agent worker `python -m app.agent.cli cycle`, backup `python -m app.ops.cli backup`, health `python -m app.monitoring.cli health`
```

## 4. Configuration (`.env.production`, never committed)

Required: `ENVIRONMENT=production`, `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD` (≥ 16 characters), `DB_NAME` (not
`*_test`, `*_staging` or `*_restore_check`), `DB_TEST_NAME` (any distinct name), `JWT_SECRET_KEY` (≥ 48 random characters:
`python -c "import secrets;print(secrets.token_urlsafe(48))"`), `ACCESS_TOKEN_EXPIRE_MINUTES` (≤ 60).

Optional: `CORS_ALLOW_ORIGINS` (`https://` only; not needed behind the bundled proxy), `TRUSTED_PROXY_IPS` (the proxy
network), `MAX_REQUEST_BODY_BYTES`, `LOGIN_MAX_FAILURES`, `LOGIN_FAILURE_WINDOW_MINUTES`, `LLM_*` (see PHASE6.md),
market-data settings (see MARKET_DATA.md).

Leave alone unless the owner has approved D15: `EXECUTION_MODE=disabled` (the default).

The API **refuses to start** in production with an unsafe combination (docs/SECURITY.md §4). A synthetic staging cost
schedule is refused outside the staging validator in every environment.

| Environment | `ENVIRONMENT` | Database | Docs | Ingestion | Execution |
|---|---|---|---|---|---|
| Local development | `development` | `agentx` | on | any authenticated user | disabled |
| Automated tests / CI | `test` (tests use `DB_TEST_NAME`, which must end in `_test`) | `agentx_test` / CI service container | on | as configured | in-process synthetic fixtures only |
| Staging (B9) | `staging` | `agentx_staging` (separate role) | off | operator | simulation only under the staging validator |
| Production | `production` | dedicated | off | operator | disabled until D15 |

## 5. Build, migrate, run

```bash
docker compose -f docker-compose.prod.yml --env-file .env.production build
docker compose -f docker-compose.prod.yml --env-file .env.production run --rm backup          # before every upgrade
AGENTX_MIGRATION_TARGET=<DB_NAME> docker compose -f docker-compose.prod.yml --env-file .env.production run --rm migrate
docker compose -f docker-compose.prod.yml --env-file .env.production up -d api web
docker compose -f docker-compose.prod.yml --env-file .env.production run --rm health
```

- Images: the API is multi-stage `python:3.14-slim`, UID 10001, with the PostgreSQL client for backups and a
  `HEALTHCHECK` on `/health/ready`. The web image is a Node 20 build stage plus `nginx-unprivileged`.
- Containers run read-only with all capabilities dropped and `no-new-privileges`, and rotate their JSON logs.
- Shutdown: uvicorn is PID 1 (exec form); on SIGTERM it finishes in-flight requests for up to 20 s
  (`stop_grace_period: 30s`). Agent and ingestion runs are one-shot processes with transactional boundaries; an
  interrupted agent cycle is marked INTERRUPTED and redone idempotently.
- The API never migrates on start-up. Until the database is at the image's head, `/health/ready` is 503 and the web
  container does not start (`depends_on: service_healthy`).

## 6. TLS and reverse proxy

Terminate TLS in front of `web` with any proxy or load balancer:
- Redirect HTTP to HTTPS.
- Use TLS 1.2+, preferably 1.3, with automatic certificate renewal (e.g. ACME).
- Forward `Host`, `X-Forwarded-For` and `X-Forwarded-Proto`, and set `TRUSTED_PROXY_IPS` to the proxy's address range
  so the API trusts only those headers.
- Keep the web port bound to localhost or a private network (the compose file binds `127.0.0.1:8080`).
- Timeouts: at least 300 s read timeout on `/api/`, because LLM explanation generation is bounded by
  `LLM_TIMEOUT_SECONDS × (LLM_MAX_RETRIES + 1)`; 15 s on headers and bodies.
- Request size: 1 MB.
- WebSockets: none are used.
- HSTS is sent by the API in production. Add it at the proxy for the static UI too, once HTTPS is confirmed.

Example with Caddy on the same host:
```
agentx.example.com {
    reverse_proxy 127.0.0.1:8080
    header Strict-Transport-Security "max-age=31536000; includeSubDomains"
}
```

## 7. Upgrade and rollback

1. Read the release notes and migrations (`backend/alembic/versions`).
2. `backup` and `verify-backup`. Note the manifest's SHA-256.
3. Build the new images and tag them with the release (`AGENTX_VERSION=<tag>`); keep the old images.
4. Stop the scheduled agent worker and ingestion jobs, or let the current one-shot finish.
5. Migrate with `AGENTX_MIGRATION_TARGET`, then `alembic check`.
6. `up -d api web`, wait for healthy, run `health`, then do a smoke test: log in, then call `/system/status`, the
   agent status and the journal.
7. Re-enable the scheduled jobs.

Rollback:
- **Code only** (the migration was additive or none): restart with the previous `AGENTX_VERSION`, but only if the
  previous code tolerates the new schema. Otherwise restore.
- **Schema:** prefer a forward fix. Where a downgrade is refused because data exists (by design), restore the
  pre-upgrade backup into an empty database (docs/BACKUP_RESTORE.md §4), accepting the loss of data written since.
- Always verify with the restore check and `/health/ready`.

## 8. Health monitoring

See docs/MONITORING.md. Point an external uptime check at `https://<host>/health/ready`, and run
`python -m app.monitoring.cli health` on a schedule during market hours (exit codes 0/1/2).

## 9. Not production-ready yet (honest status)

- No host chosen; nothing deployed; TLS, DNS, backups off-host and monitoring are not set up.
- B9 (staging validation on real fresh data) and D15 (approval of an execution environment) are **outstanding**. The
  platform is a signals-only paper system until they complete.
- Open security items: docs/SECURITY.md §7 (RLS, refresh tokens, secrets manager, front-end dependency majors).
- The CI workflows have been written and their steps run locally; they run on GitHub only after a push.
- The agent worker and ingestion rely on an external scheduler, which is not registered.
