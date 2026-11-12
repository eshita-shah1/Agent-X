# Agent-X — Security (Phase 8)

Agent-X is a paper-trading platform: there is no broker, no real-money execution and no broker credential anywhere.
This document records the Phase 8 security review (2026-11-12), the controls in place and what remains open.

## 1. Authentication

| Control | Implementation |
|---|---|
| Password hashing | argon2id (`argon2-cffi` defaults), rehashed on login when parameters change; minimum length 12 |
| Login timing | an unknown email is verified against a dummy hash, and the error is identical for unknown user and wrong password |
| Brute force (Phase 8) | after `LOGIN_MAX_FAILURES` (5) failed logins for one email within `LOGIN_FAILURE_WINDOW_MINUTES` (15): `429` with `Retry-After`, whether or not the account exists. Counted from the append-only `audit_events` (indexed), so it holds across processes and restarts and cannot be reset by rewriting history. nginx also limits `/api/v1/auth/login` to 10 requests per minute per address |
| Tokens | HS256/384/512 only (server-fixed, never taken from the token header); `sub`, `iss`, `iat` and `exp` required; secret of at least 32 characters (at least 48 and random in production); lifetime at most 60 minutes in production |
| Logout (Phase 8) | `POST /api/v1/auth/logout` sets `users.sessions_revoked_at` (migration `0014`), and every token issued at or before it is refused. This ends all of the user's sessions (no token blacklist). Tokens have 1-second `iat` resolution, so a login in the same second as the logout is also refused |
| Account lifecycle | a deactivated user (`is_active = false`) is refused on every request |
| Not implemented | refresh tokens (re-login after expiry), email verification, MFA, password reset. A `409` on duplicate registration reveals that an email is registered |

## 2. Authorization and isolation

- Identity comes only from the verified JWT. No endpoint accepts a user id, and client-supplied `user_id` fields are
  refused or ignored (tested).
- Every user-owned table uses `UserOwnedMixin` (`user_id NOT NULL`, FK to `users`). Queries go through `owned_select`,
  and `test_every_table_is_classified` forces every new table to be classified.
- Database triggers enforce ownership of cross-table links: agent records (`0012`), and journal entries, tags,
  reflections and explanations (`0013`).
- Another user's record is `404`, and a link to another user's record gets the same `422` as a missing one.
- **Operator role** (`users.is_operator`, set only by `python -m app.ops.cli set-operator`): corporate-action adjustments,
  dividend credits, `/api/v1/system/agents`, and, in staging and production, market-data ingestion. Refusals are audited
  (`operator.access_denied`).
- **Ingestion** (Phase 8): operator-only when `ENVIRONMENT` is `staging` or `production` (`INGESTION_REQUIRES_OPERATOR`
  overrides; production refuses `false`). Development keeps the approved Phase 2 local policy (any authenticated user).
- Not implemented: PostgreSQL row-level security. Isolation is enforced by the application and triggers, and the
  application role owns the tables. Before multi-tenant hosting: separate migration and runtime roles, then RLS with
  `FORCE`.

## 3. API surface

| Control | Implementation |
|---|---|
| Validation | Pydantic schemas on every request and response; unknown fields refused on Phase 5/6 bodies; bounded text, list and pagination sizes (e.g. journal `limit` ≤ 100, `offset` ≤ 10 000) |
| Body size (Phase 8) | `MAX_REQUEST_BODY_BYTES` (1 MiB) checked in middleware, for both a declared `Content-Length` and chunked bodies: `413`. nginx `client_max_body_size 1m` |
| Errors (Phase 8) | an unhandled error returns only `{"detail": "Internal server error", "request_id": ...}`. The exception and traceback are logged (redacted) with the request id. Database sessions roll back anything uncommitted |
| Security headers (Phase 8) | `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Permissions-Policy`, COOP on every response; API responses also get `Cache-Control: no-store` and `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'`; HSTS in production. The web container adds a strict CSP for the UI (`script-src 'self'`, `connect-src 'self'`) |
| CORS (Phase 8) | none by default (same origin through the reverse proxy). `CORS_ALLOW_ORIGINS` adds an explicit allow-list, which must be `https://` origins in production; no credentials, no wildcard |
| API docs | `/docs`, `/redoc` and `/openapi.json` are on in development and test, off in staging and production (`ENABLE_API_DOCS`; production refuses `true`) |
| SQL injection | SQLAlchemy parameter binding everywhere. The few raw `text()` statements use bind parameters, or table names from fixed lists (operator tooling only) |
| Files | no upload endpoints. The backup tooling writes only to an operator-chosen directory, and a restore target must be a `*_restore_check` database |
| WebSockets | none exist |
| Rate limiting | login lockout (above), the per-user hourly LLM budget (Phase 6), and nginx `limit_req` on `/api/` (20 r/s per address, burst 40). There is no in-app general limiter (it would need shared state; Redis is not approved) |

## 4. Secrets

- Secrets live only in git-ignored files (`.env`, `.env.*` except `.env.example`; `data/` holds backups and
  verification-account files) or in the deployment environment. Settings hold them as `SecretStr`.
- Logs: the JSON formatter redacts bearer tokens, JWTs, `sk-` keys, `password=`/`secret=`/`api_key=`/`token=` values and
  credentials in PostgreSQL URLs, in messages, fields and tracebacks. Fields named password, token, notes, prompt and
  similar are replaced by `<redacted>`. The request log omits query strings, which could carry private journal search
  text.
- **Production start-up refuses** a weak or placeholder `JWT_SECRET_KEY` (fewer than 48 characters or low variety), a
  `DB_PASSWORD` shorter than 16 characters, a test, staging or restore-check `DB_NAME`, tokens longer than 60 minutes,
  docs enabled, wildcard or non-https CORS, open ingestion, or a fixture market-data provider. The error names the
  setting, never its value.
- The frontend bundle contains no backend secret; CI greps the build output.
- Docker images contain no `.env` (`.dockerignore`, verified in CI) and receive configuration at run time.
- **Git history scan (2026-11-12, all 72 commits on every branch):** no credentials found.
  - Matches were placeholders in `.env.example` and documentation, PowerShell code that reads the git-ignored
    `.env.staging`, and a deliberate test value in `tests/unit/test_config.py` (URL-escaping test).
  - No `.env`, key, dump or credential file was ever committed.

## 5. Dependencies (reviewed 2026-11-12)

Python: every package is pinned exactly (`requirements.txt`, `requirements-dev.txt`). **`pip-audit`: no known
vulnerabilities.** Dev-only tools: pytest, httpx (TestClient), ruff.

npm (`npm audit`, lockfile-pinned): 5 findings, all fixed only by a **major** upgrade. Each is already on the newest
release of its current major version. No blind upgrade was made. Assessment:

| Package | Severity | Where it runs | Exposure in Agent-X | Decision |
|---|---|---|---|---|
| vite 5.4.21 (path traversal in optimized deps; launch-editor UNC hash leak) | high | dev server only | not in the production bundle; the dev server binds to localhost | keep; never expose `npm run dev`; plan Vite 6+ upgrade with a regression pass |
| esbuild 0.21.5 (dev server cross-origin reads) | moderate | dev server only (via vite) | as above | as above |
| react-router(-dom) 6.30.6 (open redirect via backslash in `<Link>`/`useNavigate`; SSR deserialisation) | moderate | browser | navigation targets are static or backend dates; no SSR | keep; plan v7 migration |
| zod 3.25.76 (memory exhaustion on oversized constrained collections) | high | browser (form validation) | validates the user's own form input in their own browser | keep; plan zod 4 with `@hookform/resolvers` |

CI fails on any Python vulnerability and on any critical npm vulnerability, and re-runs these audits on every change.

## 6. Trading safeguards (unchanged by Phase 8)

Phase 8 adds no trading authority:
- Execution stays disabled by default and requires an approval reference (D15 is open).
- Synthetic staging cost schedules are refused outside the staging validator.
- The kill switch and the risk engine remain authoritative.
- LLM output can never trade (Phase 6 boundary tests).
- No broker integration or broker credential exists.

## 7. Open items before hosted deployment

1. Choose a hosting provider and TLS termination (docs/DEPLOYMENT.md §1); none is selected.
2. PostgreSQL RLS with separate migration and runtime roles.
3. Refresh tokens or shorter sessions with re-authentication, and email verification.
4. A secrets manager instead of an env file, with JWT key rotation (key ids).
5. Major upgrades of vite, react-router and zod (§5).
6. An audit-log retention policy, and a PII review of `auth.login_failed` emails.
