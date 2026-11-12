# Phase 8 — Production Hardening, Reliability, Security and Deployment Readiness

Status: implemented and tested on branch `phase-8-hardening` (from `main` @ `f8f8000`; not committed, pushed or
deployed). Phase 8 adds no trading authority:
- execution stays disabled;
- the kill switch and risk engine are unchanged and authoritative;
- the LLM remains explanatory only;
- no broker, scheduler, queue or thread was added.

**B9** (Phase 4 staging validation) and **D15** (agent execution approval) remain separate, outstanding milestones;
Phase 8 does not complete them.

## 1. Compatibility (Stage 8.0, 2026-11-12)

- **Git:** Phase 6 is merged into `main` (PR #6, `f8f8000`). Phases 1–6 match the documentation. The only untracked
  file is the owner's Phase 5 evidence note (left untouched).
- **Migrations:** one head (`0013`) before Phase 8. Development `agentx` and `agentx_test` were both at `0013`.
- **Existing posture:**
  - argon2id with timing equalisation, and strict JWT validation;
  - `owned_select` plus ownership triggers, and an operator role for corporate actions and dividends;
  - JSON logs; `/health` and `/health/ready` checked the database only;
  - a single-stage Docker image (non-root); no CI.
- **Gaps found:**
  - no logout or session revocation, and no login brute-force protection;
  - no security headers, body limit, request ids or production configuration guard;
  - `/docs` always exposed, and ingestion open to any user in every environment;
  - readiness blind to schema drift; no operator view of agents;
  - no backup or restore tooling; a dev-only Docker setup.

## 2. What changed

| Stage | Change | Files |
|---|---|---|
| 8.1 Security | Logout revoking all sessions (migration `0014`); login lockout from the audit log; production start-up rules; security headers, 413 cap, safe 500s, optional CORS, docs off outside development and test; operator-only ingestion in staging and production; git history scan; dependency report | `core/config.py`, `core/security.py`, `core/http.py`, `api/v1/auth.py`, `api/v1/deps.py`, `api/v1/market.py`, `main.py`, `alembic/versions/0014_*`, `db/models/user.py`, `docs/SECURITY.md` |
| 8.2 Observability | Context-bound JSON logs with redaction (request, agent and cycle ids); readiness requires the migration head; `/system/status`, `/system/agents`, `python -m app.monitoring.cli health` | `core/logging.py`, `monitoring/`, `api/v1/system.py`, `agent/cycle.py` (log context only), `docs/MONITORING.md` |
| 8.3 Recovery | Manifest-verified backups (SHA-256, TOC, row checksums), isolated `*_restore_check` restore with comparison, retention, alembic wrong-database guard, admin provisioning script | `ops/backup.py`, `ops/cli.py`, `alembic/env.py`, `scripts/recovery/provision_restore_check.sql`, `docs/BACKUP_RESTORE.md` |
| 8.4 Performance | Lockout index (measured); route-level code splitting (measured) | `0014`, `frontend/src/App.tsx` |
| 8.5 Tests | `tests/unit/test_phase8_units.py`, `tests/integration/test_phase8_hardening.py`, `tests/phase8/test_boundaries.py` | — |
| 8.6 Docker | Multi-stage non-root API image with PostgreSQL 18 client tools and a healthcheck; nginx-unprivileged web image with CSP, rate limits and caching; provider-neutral `docker-compose.prod.yml` (read-only, no capabilities, explicit migrate and backup tools) | `backend/Dockerfile`, `frontend/Dockerfile`, `frontend/nginx.conf`, `docker-compose.prod.yml`, `.dockerignore` |
| 8.7 CI/CD | CI: ruff, migrations and drift, full tests on a throwaway PostgreSQL 18, frontend lint and build, pip-audit and npm audit, secret checks, Docker builds. Release: manual, environment-gated image build, no deploy | `.github/workflows/ci.yml`, `release.yml`, `backend/pyproject.toml`, `requirements-dev.txt`, `frontend/package.json` |
| 8.8 Docs | PHASE8, SECURITY, DEPLOYMENT, BACKUP_RESTORE, MONITORING; README, ARCHITECTURE, API, OPERATIONS, FRONTEND, SETUP, `.env.example` | `docs/` |

## 3. Security findings and remediation

| # | Finding | Severity | Remediation | Status |
|---|---|---|---|---|
| S1 | No logout or revocation: a leaked token is valid until expiry | high | `POST /auth/logout` and `sessions_revoked_at` | fixed, tested |
| S2 | No login brute-force protection | high | per-email lockout from audit rows, plus nginx limit | fixed, tested |
| S3 | Production could start with placeholder secrets, docs open, or a test DB | high | `production_config_problems` (fatal) | fixed, tested |
| S4 | Any user could start ingestion | medium (documented Phase 2 local policy) | operator-only in staging and production | fixed, tested |
| S5 | No security headers, CSP or body limit | medium | middleware and nginx | fixed, tested |
| S6 | Unhandled errors without a request reference | low | safe JSON 500 with `request_id`; redacted, logged traceback | fixed, tested |
| S7 | Secrets could appear in logs (exception text, URLs) | medium | redaction filter | fixed, tested |
| S8 | npm: vite/esbuild (dev server), react-router, zod | high/moderate | assessed not exploitable as shipped; majors planned | accepted, documented |
| S9 | No RLS; refresh tokens; secrets manager | medium | — | open (SECURITY.md §7) |
| S10 | Git history | — | scanned all 72 commits: no credentials | verified |

## 4. Measurements

| What | Before | After | Notes |
|---|---|---|---|
| Login-lockout count, 100k failed-login rows (median of 20) | 83.856 ms, sequential scan, 1,235 buffers | 0.108 ms, bitmap index scan, 4 buffers | partial expression index in `0014` |
| Initial JavaScript, gzip: Journal / Insights / Dashboard / Trading / Backtesting | 366.8 KiB every page | 140.4 / 133.8 / 189.4 / 220.5 / 252.7 KiB | −62 / −64 / −48 / −40 / −31 % |
| Entry chunk (raw) | 1,304 kB | 380 kB | route-level `React.lazy` |
| Session candles (dev, read-only) | 2.1 ms index | — | no change needed |
| Latest bar for the price gate (dev) | 0.11 ms index | — | no change needed |
| 60-day coverage scan (dev) | 3.9 ms index | — | no change needed |
| Agent decisions page (dev) | 0.10 ms index | — | no change needed |
| Journal list, 20k entries (test) | 0.49 ms | — | no change needed |
| Journal search, common term, 20k entries (test) | 0.21 ms | — | no change needed |
| Journal search, no-match AND query, 20k entries for one user (test) | 18.2 ms | — | ordered walk; acceptable at this scale; revisit if a user exceeds about 100k entries |
| Docker images | 1 stage | API 806 MB (python-slim, pandas/numpy, PG18 client); web 75.5 MB | builds 3m21s / 1m43s (cold) |

Not measured: memory and CPU under load (no load test was run). DEPLOYMENT.md labels its sizing as estimates.

## 5. End-to-end test plan (where each workflow is tested)

All suites run on `agentx_test` or in-process fixtures; none touches development or staging.

| Workflow | Coverage |
|---|---|
| Users: registration, login, me, account, logout, unauthorized, cross-user | `test_auth.py`, `test_account.py`, `test_isolation.py`, `test_phase8_hardening.py` (logout, lockout, deactivated, 401s) |
| Market data: ingestion, duplicates, missing and stale data, provider failure, provenance | `test_market_service.py`, `test_market_jobs.py`, `test_market_api.py`, `test_phase2_finalization.py`, `tests/marketdata/` |
| Backtesting: configuration, submission, execution, results, Buy-and-Hold, limitations | `tests/backtest/`, `test_backtest_api.py`, `test_backtest_schema.py` (assumptions unchanged) |
| Mock Exchange: orders, risk, accept/reject, partial and complete fills, fees, cash, positions, protective exits, ledger | `test_exchange.py`, `test_simulation_execution.py`, `test_phase4_*.py`, `test_trading_api.py`, `tests/unit/test_costs_and_accounting.py` |
| Agent: lifecycle states, SIGNALS_ONLY, kill switch, cycles, interrupted cycles, restart, duplicate worker, freshness | `test_agent_cycle.py`, `test_agent_api.py`, `tests/agent/`; observability in `test_phase8_hardening.py` |
| Phase 6: journal, sync, explanations, fallback, LLM failure, search, isolation, insights | `test_phase6_records.py`, `test_phase6_api.py`, `tests/phase6/`, `test_llm_narrator.py` |
| Hardening: headers, 413, 500, CORS, docs, readiness, status, backups, migration guard | `test_phase8_hardening.py`, `test_phase8_units.py`, `tests/phase8/` |
| Containers | built and run locally with production flags (§6) |

## 6. Verification record (2026-11-12, Windows, PostgreSQL 18.6, Docker 29.2.0)

- **Full backend suite** on `agentx_test` (`-W default`): **1670 passed, 1 failed, 1 skipped, 1 warning, 28m20s**.
  - The failure was the Phase 2 start-up guard. It reads `main.py` text between `lifespan` and `FastAPI(` and caught a
    harmless `docs = ...` line placed there. The line was moved and the guard left unchanged.
  - The affected and related tests were then re-run: 78 passed, 1 skipped.
  - The 5 Phase 8 boundary tests were written after the run started and pass separately.
  - The skip is the live restore (provisioning pending). The warning is the existing `httpx` TestClient deprecation.
  - Baseline before Phase 8: 1615 (Phase 6). Phase 8 added 62 tests (57 in this run, plus the 5 boundary tests).
- **Migrations:** round trip `0001 → 0014 → base → head` with no model drift. One head (`0014`).
- **Lint:** `ruff check .` passes (correctness rules); `npm run lint` and `npm run build` pass.
- **Dependencies:** `pip-audit`, no known vulnerabilities; `npm audit`, 5 findings (0 critical), assessed in SECURITY.md §5.
- **Containers**, run with `--read-only`, `--cap-drop ALL` and `no-new-privileges`, against `agentx_test`,
  `ENVIRONMENT=staging`:
  - API and web are both `healthy`; readiness through the proxy is `ready`;
  - `/api` without a token gives 401, and the API docs give 404 (off in staging);
  - a 2 MB body gives 413, and login gives 429 after 5 failures;
  - the SPA deep link works, the UI renders under the CSP on 4 routes, and lazy chunks load;
  - API and UI security headers are single-sourced;
  - logs are JSON with request ids;
  - the image runs as UID 10001 with `pg_dump` 18.6 and no `.env`.
- **CI:** the workflows were written, and each step was run locally (lint, migrations, tests, builds, audits, Docker).
  They have **not run on GitHub**: that needs a push, which is not approved.
- **Isolated restore demonstration:** tooling implemented and tested (backup, verify and tamper detection on
  `agentx_test`). The live restore into `agentx_restore_check` waits for the administrator script
  (BACKUP_RESTORE.md §3); its test skips until then.

## 7. Acceptance status

| # | Criterion | Status |
|---|---|---|
| 1 | Security review, critical findings resolved | done (no critical; highs fixed or assessed) |
| 2 | User isolation verified across modules | verified (existing and new tests) |
| 3 | Secrets protected | done (history scan clean; redaction; production guard) |
| 4 | Structured logging consistent | done |
| 5 | Health and readiness correct | done, tested |
| 6 | Agent failures and stale workers observable | done, tested |
| 7 | Backup and restore demonstrated in isolation | **backup and verification demonstrated; live restore pending the admin script** |
| 8 | Migration safety verified | done (round trip, guard, readiness) |
| 9 | Existing trading records unchanged | development DB untouched by Phase 8 (read-only measurements only) |
| 10 | Bottlenecks measured, addressed where justified | done (§4) |
| 11 | Full backend regression passes | done: 1670 passed; the one failure (a source-text guard) fixed and re-verified |
| 12 | Frontend production build passes | done |
| 13 | Docker production build succeeds | done (and run) |
| 14 | CI workflows pass | **steps pass locally; not run on GitHub (needs an approved push)** |
| 15 | Deployment documentation complete | done (provider-neutral) |
| 16 | No trading safeguard weakened | verified (boundary tests) |
| 17 | Execution remains disabled | verified |
| 18 | B9 and D15 reported as outstanding | yes |

**Owner actions:**
- approve Phase 8;
- migrate the development database to `0014` (backup first);
- run the restore-check provisioning script;
- choose a hosting provider (DEPLOYMENT.md §1);
- configure the GitHub `release` environment's reviewers;
- B9 and D15 (separate milestones).
