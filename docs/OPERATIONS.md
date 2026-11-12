# Agent-X — Operations (Phase 4)

Paper trading only. Execution is disabled unless `EXECUTION_MODE` is explicitly enabled with an approval reference
(see [D4_EXECUTION_READINESS.md](D4_EXECUTION_READINESS.md)). All commands run from `backend/`.

## 1. Daily market-data ingestion (B6)

Agent-X never schedules itself: Phase 2 decision 5 forbids an in-app scheduler, and its guard tests enforce this.
`python -m app.ops.cli daily` is a **one-shot, idempotent** command meant to be run by an external scheduler.

What one run does:
1. **Seed check:** registers the approved instruments and provider; idempotent.
2. **One incremental ingestion job over every enabled equity**, using Phase 2's job machinery:
   - starts from the last canonical bar;
   - overlaps 24 h to detect restatements;
   - reports an `UNRECOVERABLE_GAP` if the archive fell out of Yahoo's window;
   - records corporate actions for every batch;
   - stores finalised bars only;
   - quarantines invalid rows.
3. **A coverage and freshness report:** missing intervals, partial or absent days, unexplained absences.

| Exit code | Meaning |
|---|---|
| 0 | SUCCEEDED (coverage may still be incomplete: read `coverage_complete`) |
| 2 | PARTIAL (some instruments failed) |
| 1 | FAILED |
| 3 | another ingestion job is alive (overlap refused) |

Restart safety:
- A job whose heartbeat stopped for longer than `MARKETDATA_JOB_HEARTBEAT_TIMEOUT_SECONDS` is marked `INTERRUPTED` by
  the next run, which then proceeds.
- Re-running is always safe: archiving is idempotent.

**A successful run is not proof of complete coverage.** In the first real run on 2026-11-11, every instrument
succeeded, yet each showed about 83 missing 5-minute intervals over 10 days: Yahoo gap markers, which are quarantined
and never filled.

### Scheduling examples
Run with the project's virtual environment and `.env`. `--report-dir` keeps a JSON report per run.

macOS / Linux (cron, IST machine clock): every 5 minutes during the session, plus one run after the close.
```
*/5 9-15 * * 1-5  cd /path/Agent-X/backend && ../.venv/bin/python -m app.ops.cli daily --report-dir ../data/ingestion_reports >> ../data/ingestion.log 2>&1
40 15 * * 1-5     cd /path/Agent-X/backend && ../.venv/bin/python -m app.ops.cli daily --report-dir ../data/ingestion_reports >> ../data/ingestion.log 2>&1
```

Windows (Task Scheduler): the approved 5-minute cadence is packaged in the repository.
```powershell
# one ingestion run (refuses outside Mon-Fri 09:15-15:40 IST; -IgnoreMarketHours for a manual test only)
powershell -ExecutionPolicy Bypass -File scripts\windows\ingest_intraday.ps1
# register "AgentX-Intraday-Ingestion": every 5 minutes, Mon-Fri 09:15-15:40, current user (machine on IST)
powershell -ExecutionPolicy Bypass -File scripts\windows\register_ingestion_task.ps1
# remove it
Unregister-ScheduledTask -TaskName "AgentX-Intraday-Ingestion" -Confirm:$false
```
`ingest_intraday.ps1` wraps `python -m app.ops.cli daily --coverage-days 1`, appends one line per run to
`data\ingestion.log` (failures with the tail of the output) and keeps each JSON report in `data\ingestion_reports\`.
It never creates, edits or back-dates candles: every bar keeps Yahoo's timestamps and its real `observed_at`.
Run-tested on the Windows integration machine on 2026-11-11 (SKIPPED outside hours; manual run SUCCEEDED).
- The cadence is an operational decision (§2).
- Overlapping runs are refused (exit 3), so a short interval is safe.

## 2. Latency and the staleness threshold (B7)

`EXECUTION_MAX_PRICE_AGE_SECONDS` (900 s: the B7 development decision of 2026-11-11, paired with a 5-minute ingestion cadence; still to be confirmed by the measurement below) is the maximum age of the reference bar at decision time.
- A bar is only usable once Agent-X has archived it.
- So the age at decision is at least: Yahoo's publication delay + the finalisation margin (60 s, provisional) + the
  time until the next ingestion run.

**Measurement procedure**
1. During market hours, run ingestion on the intended cadence for at least 5 sessions.
2. Run `python -m app.ops.cli latency --days 5`. It reports the distribution of `observed_at − bar_end` over
   archived bars.
3. Optionally, run the Phase 2 probe during market hours (`python -m app.marketdata.cli probe --persist`) to measure
   Yahoo's own settle time separately.
4. **Choose the threshold:** `p99(observed_at − bar_end) + one bar (300 s) + an explicit safety margin`. It must
   stay **well below** the horizon over which a 5-minute strategy's signal is still meaningful. Record the measured
   figures and the chosen value with the approval reference.

**Finding (2026-11-11, Mac dev archive, backfilled after the close):** the lag was at least 16,639 s, because a
backfill observes old bars late. This is not a latency measurement; it shows that **with once-a-day ingestion every
reference price would be stale under 900 s**, and no order could ever be accepted. Simulation requires intraday
ingestion (§1) or an explicitly approved, larger threshold.

- **Slippage:** the `SLIPPAGE` entry in the cost schedule (adverse bps on the bar open), effective-dated and recorded
  on every fill.
- **Stale rejections:** recorded on the order (`reject_reason` contains `STALE` and the age) and in the risk decision
  log (`risk_decisions.price_check`).
- No threshold or slippage value is presented as empirically validated until the procedure above has been run and
  approved.

**Freshness check** (read-only): `python -m app.ops.cli freshness --min-run 8` reports, per instrument, the longest run
of contiguous bars recorded within `EXECUTION_MAX_PRICE_AGE_SECONDS` of their close (exit 0 if some run reaches
`--min-run`, else 2). The staging validator needs such a run; on 2026-11-12 00:00 IST the best run was 3.

### Protective-exit alerts (migration 0011)
When a protective-exit run cannot evaluate a position's stop/target (stale or ineligible price), an OPEN requirement
and an audit event `risk.protective_exit_unresolved` are recorded. Operators review them with
`GET /api/v1/operator/protective-exits/unresolved`. They resolve on the first protective-exit run with a fresh eligible
price (an exit is submitted if the level was crossed, including a crossing already seen on the stale bar). A
requirement that stays open usually means ingestion has stopped: check `data\ingestion.log` and `ops freshness`.

## 3. Operators

- Operators may apply corporate-action adjustments and dividend credits system-wide. Grant or revoke on the
  **server only**:
  `python -m app.ops.cli set-operator --email ops@example.com --on` (or `--off`).
- The `is_operator` flag cannot be set through the API.
- Every grant and revoke is audited (`operator.granted` / `operator.revoked`), as is every denied API attempt
  (`operator.access_denied`, 403).

## 4. Corporate actions: splits and bonuses (B4)

1. `GET /api/v1/operator/corporate-actions/pending` lists the actions freezing positions, with a count of affected
   positions.
2. `POST /api/v1/operator/corporate-actions/{id}/adjustments` applies the action to **every** affected position:
   - Body: `evidence_reference` plus a re-entered `confirm_ratio_numerator` / `confirm_ratio_denominator`.
   - If any entitlement is fractional, also `cash_in_lieu_price`, `cash_in_lieu_source` and
     `cash_in_lieu_observed_at`. The price is never estimated.
3. Each user's result (APPLIED, REJECTED or BLOCKED_FRACTIONAL) is recorded with the operator's id. Users see their
   own records read-only (`GET /api/v1/portfolio/corporate-actions/...`).

## 5. Dividends (B5)

1. `GET /api/v1/operator/dividends/pending` lists dividends whose ex-date has passed, with the number of entitled
   users (and users blocked by an unadjusted split).
2. `POST /api/v1/operator/dividends/{id}/credits` takes `evidence_reference`, `confirmed_amount_per_share`,
   `payment_date` and an optional `accept_provider_difference`.
3. **Entitlement:** shares held at the close of the last session before the ex-date, replayed from immutable fills
   and applied split adjustments.
4. **Credit:** gross amount on the payment date, as a separate `DIVIDEND` ledger entry. Once per user; re-running
   returns the existing credit.
5. **Refused, recorded and nothing credited:**
   - a future or pre-ex payment date;
   - missing evidence;
   - an amount that differs from the provider record unless explicitly accepted;
   - a holding across an unadjusted split.
6. **Limitation:** no TDS is deducted.

## 6. Cost schedule validation (B2)

- **Broker:** Zerodha (owner decision). A production schedule is built **only** from Zerodha's official delivery
  tariff and real contract notes supplied by the owner.
- **Check a note:** `python -m app.ops.cli check-contract-note path/to/note.json [--schedule schedule.json]`. Exit 0
  means every component matches exactly.
- **Golden tests:** place redacted notes in `backend/tests/golden/contract_notes/`, using the format in
  [tests/golden/README.md](../backend/tests/golden/README.md). They become tests automatically.

## 7. Agent worker (Phase 5)

Paper trading only; design in [PHASE5.md](PHASE5.md). The agent never schedules itself (decision D8):
`python -m app.agent.cli cycle` runs **one** cycle for every configured agent and exits.

| Step | Who | Command |
|---|---|---|
| Back up and migrate the database to `0012` | owner | `alembic upgrade head` (from `backend/`) |
| Configure and start the agent | user | `PUT /api/v1/agent/config`, `POST /api/v1/agent/start` (or the dashboard's Agent control panel) |
| Register the 5-minute worker task, 2 minutes after ingestion | owner | `powershell -ExecutionPolicy Bypass -File scripts\windows\register_agent_task.ps1` |
| Run one cycle by hand | operator | `powershell -ExecutionPolicy Bypass -File scripts\windows\agent_cycle.ps1 -IgnoreMarketHours` |
| Inspect | operator | `python -m app.agent.cli status`, `data\agent.log`, `GET /api/v1/agent/status`, `/agent/cycles`, `/agent/events` |
| Remove the task | owner | `Unregister-ScheduledTask -TaskName "AgentX-Agent-Cycle" -Confirm:$false` |

Exit codes of `cycle`: `0` every cycle succeeded or was skipped (another worker held that agent), `1` a cycle failed
(recorded in `agent_cycles` with the error; the next run retries; 3 consecutive failures put the agent in ERROR).

- **Fresh data comes only from ingestion** (`AgentX-Intraday-Ingestion`, §1). The agent never ingests. If ingestion
  stops, entries are refused as stale and owed exits wait for an eligible price; protective-exit requirements are
  recorded by Phase 4 (§2).
- **Execution stays disabled in development.** There, a started agent is `SIGNALS_ONLY`: it records signals and never
  submits an order. Enabling simulated execution remains the D4 procedure (D4_EXECUTION_READINESS.md §7); end-to-end
  agent paper trading needs its own approval (D15, deferred).
- **Restarts:** the API and the worker restart independently. A crashed cycle leaves no partial orchestration work; its
  row is marked INTERRUPTED by the next cycle. Two workers never cycle the same agent at once (advisory lock).
- **`WORKER_NOT_SEEN`** in the agent status means no cycle has run for `AGENT_WORKER_STALE_AFTER_SECONDS` (900 s): check
  the scheduled task and `data\agent.log`.
- **Risk settings** can be changed only while the agent is not RUNNING (`409 AGENT_RUNNING`).
- **Orphaned positions** (`ORPHANED_OVERNIGHT`, an agent position whose 15:15 square-off did not fill) are never sold
  by the agent the next day. They stay under their Phase 4 stop-loss/take-profit; close them manually if required.

## 8. Explanations and the trading journal (Phase 6)

Design: [PHASE6.md](PHASE6.md). Nothing here runs on a schedule and nothing here can trade.

1. **Migration (owner action).** Back up and migrate the development database to `0013`, following the `0012`
   procedure (pg_dump, `alembic upgrade head`, `alembic check`, and before/after counts and checksums of orders, fills,
   candles and risk settings). `0013` is additive: no existing row changes. Its downgrade is refused once journal or
   explanation rows exist; to roll back, restore the backup.
2. **LLM provider (optional).** Leave `LLM_PROVIDER=disabled` for rule-based explanations only. To use a local model:
   set `LLM_PROVIDER=openai_compatible`, `LLM_BASE_URL=http://127.0.0.1:11434/v1` and `LLM_MODEL=...` (Ollama or
   LM Studio; no key, no cost). To use Claude: `pip install anthropic`, then set `LLM_PROVIDER=anthropic` and
   `LLM_API_KEY=...`. Check the result with `GET /api/v1/explanations/provider`: `enabled`, `configuration_problems`.
   A misconfigured provider never stops the application; it is reported there and explanations stay rule-based.
3. **Journal entries.** `POST /api/v1/journal/sync` (the Journal page calls it on open) creates the missing automatic
   entries for the last 30 days. It is idempotent and safe to repeat. A session summary entry appears only after
   15:30 IST.
4. **Privacy.** With an external provider, prompts contain trading facts only (symbols, prices, ids). Journal notes
   are sent only if `LLM_INCLUDE_JOURNAL_NOTES=true`. Logs (`agentx.llm`) record provider, model, status, error code and
   latency, never prompts or keys.
5. **Cost and abuse bound.** At most `LLM_MAX_REQUESTS_PER_USER_PER_HOUR` LLM calls per user per hour (stored
   explanations, including failed ones, plus reflection drafts). Beyond that, responses are `RATE_LIMITED` and
   rule-based.
6. **Retention.** Stored explanations and automatic entries are kept, and no job deletes them. Manual entries are
   deleted only by their owner.

## 9. Hardening operations (Phase 8)

Details: [BACKUP_RESTORE.md](BACKUP_RESTORE.md), [MONITORING.md](MONITORING.md), [DEPLOYMENT.md](DEPLOYMENT.md).

| Task | Command (from `backend/`) | Who |
|---|---|---|
| Backup with manifest | `python -m app.ops.cli backup --label <why>` | operator |
| Verify a backup | `python -m app.ops.cli verify-backup <dump>` | operator |
| Isolated restore check | `python -m app.ops.cli restore-check <dump> --target agentx_restore_check` (after the admin script) | operator |
| Retention | `python -m app.ops.cli prune-backups [--apply]` | operator |
| Health probe | `python -m app.monitoring.cli health` (exit 0/1/2) | monitor |
| Migrate staging/production | `AGENTX_MIGRATION_TARGET=<db> alembic upgrade head` after a verified backup | owner |
| End all sessions of a user | the user calls `POST /api/v1/auth/logout`; an operator can deactivate (`users.is_active=false`) | user / operator |

Nothing above is scheduled automatically. Registering a scheduled backup or health probe is an owner decision (as for
ingestion and the agent worker).

**Pending owner action:** migrate the development database `agentx` from `0013` to `0014` (backup first,
BACKUP_RESTORE.md §6). Until then this branch's code refuses authenticated requests against it, and `/health/ready`
reports `unavailable`.
