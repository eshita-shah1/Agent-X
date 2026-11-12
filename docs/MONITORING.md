# Agent-X — Monitoring and Observability (Phase 8)

## 1. Health endpoints

| Endpoint | Auth | Meaning | Failure |
|---|---|---|---|
| `GET /health` | public | liveness: the process answers (no database) | process down |
| `GET /health/ready` | public | readiness: the database answers **and** its schema is at this code's migration head | `503 {"status": "unavailable"}`. Public, so it reveals nothing more |
| `GET /api/v1/system/status` | user | `readiness` (database, migration current/expected/compatible) and `reported` (execution configuration, market-data freshness per provider, the optional LLM provider, the caller's own agent) | `overall: unavailable` only for database or migration problems |
| `GET /api/v1/system/agents` | operator | every agent: status, mode, heartbeat, last successful cycle, last cycle status and error code, consecutive failures, stale worker, cycles in 24 h by status, `attention` list | — |
| `python -m app.monitoring.cli health` | server | the same as JSON, for external monitors and schedulers | exit 0 ok, 1 an agent needs attention, 2 not ready |

What deliberately does **not** fail readiness:
- `EXECUTION_MODE=disabled` is the approved development state, reported with `is_failure: false`.
- An unconfigured or failing LLM provider has `affects_readiness: false`; explanations fall back to rule-based.
- Stale market data is a data condition: the price gate refuses entries and the agent shows blockers.

## 2. Agent observability (from Phase 5 records, no parallel state)

`attention` flags:
- `AGENT_ERROR: <reason>`
- `WORKER_NOT_SEEN`: a RUNNING or PAUSED agent with no heartbeat for `AGENT_WORKER_STALE_AFTER_SECONDS` (900 s); this
  uses the agent's own rule
- `CONSECUTIVE_FAILURES: n`
- `INTERRUPTED_CYCLES_24H: n`

Also visible:
- `cycles_24h` (RUNNING, SUCCEEDED, FAILED, INTERRUPTED);
- `last_successful_cycle_at`;
- `last_cycle_error_code`.

Data-freshness, execution-disabled, kill-switch and risk blockers stay in `GET /api/v1/agent/status` (`blockers`) and
the agent's decision outcomes (`SUPPRESSED_PRICE_NOT_ELIGIBLE`, `SUPPRESSED_KILL_SWITCH`, ...).

## 3. Logs

Every line is one JSON object (`app/core/logging.py`):

| Field | When |
|---|---|
| `ts`, `level`, `logger`, `message` | always |
| `request_id` | every API request. A safe incoming `X-Request-ID` is kept, otherwise generated, and returned in the response |
| `method`, `path`, `status`, `duration_ms`, `user_id` | the `agentx.http` request line (no query strings) |
| `agent_id`, `worker_id`, `cycle_id` | every line inside an agent cycle |
| `error_category`, `exc` | errors (exception class; redacted traceback) |
| `provider`, `model`, `status`, `latency_ms` | LLM narratives (`agentx.llm`; never prompts or keys) |

Redaction: bearer tokens, JWTs, API keys, `password`/`secret`/`token` values and credentials in database URLs are
replaced with `<redacted>`. uvicorn's plain-text access log is disabled in favour of the structured request line.

## 4. What to alert on (suggested)

| Signal | Source | Suggested threshold |
|---|---|---|
| Not ready | `/health/ready` or `health` CLI exit 2 | 2 consecutive checks (1 min) |
| Agent needs attention | `health` CLI exit 1 / `/system/agents` | any, during market hours |
| Worker not seen | `WORKER_NOT_SEEN` | > 15 min while RUNNING |
| Market data stale | `/system/status` `minutes_since_ingestion` | > 15 min during market hours |
| Provider failing | `consecutive_failures` ≥ 3 | any |
| Error rate | `level=ERROR` lines / `status` ≥ 500 | > 1 % of requests over 5 min |
| Login lockouts | `429` on `/auth/login` | spike above baseline |
| Backup missing | newest manifest age | > 26 h |

No monitoring service is registered or configured by Phase 8 (none is approved). The CLI and endpoints are ready for
whichever is chosen.
