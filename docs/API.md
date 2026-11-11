# Agent-X Backend — API (Phase 1)

Base path `/api/v1`. Interactive docs at `/docs` (OpenAPI at `/openapi.json`).
Authenticated endpoints need `Authorization: Bearer <access_token>`.

## Health

| Method | Path | Auth | Response |
|---|---|---|---|
| GET | `/health` | none | `200 {"status":"ok"}` (process is up) |
| GET | `/health/ready` | none | `200 {"status":"ready"}` or `503 {"status":"unavailable"}` (database check) |

## Auth

### `POST /api/v1/auth/register` → 201
```json
{ "email": "user@example.com", "password": "at-least-12-characters", "initial_capital": "500000.00" }
```
- `initial_capital` is **required**, positive, at most 2 decimal places, and has no default.
- Email is trimmed and lower-cased; duplicates return `409`. Invalid input returns `422`.
- Creates the user and its single virtual account (currency `INR`) in one transaction.
- Response: `{ "user": {id,email,is_active,created_at}, "account": {id,currency,initial_capital,created_at} }`.

### `POST /api/v1/auth/login` → 200
```json
{ "email": "user@example.com", "password": "..." }
```
Response: `{ "access_token": "...", "token_type": "bearer", "expires_in": <seconds> }`.
Wrong password and unknown email both return the same `401`.

### `GET /api/v1/auth/me` → 200
The authenticated user. `401` for a missing, malformed, expired, tampered, or unknown-user token, or an inactive user.

## Account

### `GET /api/v1/account` → 200
The authenticated user's own virtual account `{id,currency,initial_capital,created_at}`. No user id is accepted; extra query parameters or headers are ignored.

## Errors
Errors use FastAPI's default `{"detail": ...}` shape. `401` responses carry `WWW-Authenticate: Bearer`.

## Not yet available
Instruments, market data, strategies, agent, risk, exchange, portfolio, backtests, benchmarks, journal and reports arrive in later phases.
