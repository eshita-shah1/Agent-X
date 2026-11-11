# Staging validation (B9)

Validates the Phase 4 business flow against a **copy** of the real Yahoo archive, in **simulation-only** mode, on a
dedicated database (`agentx_staging`) owned by a dedicated least-privilege role (`agentx_staging_app`). The original
database is only read (`pg_dump`). Development (`agentx`) stays `EXECUTION_MODE=disabled`. Real-money execution does
not exist.

**Approved staging decisions (2026-11-12):** Yahoo Finance; `UNVERIFIED_PROVIDER_NATIVE`; simulation only; DELIVERY
only; 5-minute ingestion during market hours; 15-minute maximum price age; synthetic, staging-only cost schedule;
approval reference `STAGING-ONLY:AGENTX-MOCK-EXCHANGE-VALIDATION-001` (authorises only this isolated exercise);
approval references immutable on accepted orders and fills; pending orders cancelled `EXECUTION_APPROVAL_CHANGED` when
the active approval changes.

## Status (2026-11-12 00:30 IST)

| Step | State |
|---|---|
| Code (timing fix, approval references, schedule guard, stale protective exits, lifecycle runner) | Done, tested |
| Development database migrated to `0011` | Done ([evidence](evidence/2026-11-12_dev_migration_0011.txt)) |
| Ingestion wrapper and scheduler registration script | Done; wrapper run-tested. **Task registration: pending, run by the owner** (§1) |
| Fresh intraday data (≥ 8 consecutive bars within 900 s) | **Pending the next NSE session.** Current best run: 3 bars |
| Staging role and database | **Pending an administrator** (§2) |
| Copy, validator runs, lifecycle exercise | Blocked on the two rows above; scripts ready (§3-§5) |

Nothing below has been reported as passed until it ran. Exit code 2 (no fresh scenario) is **not** a pass.

## 1. Collect fresh intraday data (owner, once)
```powershell
# registers "AgentX-Intraday-Ingestion": every 5 minutes, Mon-Fri 09:15-15:40 IST, as the current user
powershell -ExecutionPolicy Bypass -File scripts\windows\register_ingestion_task.ps1
```
The task runs `scripts\windows\ingest_intraday.ps1`, which wraps `python -m app.ops.cli daily` (Agent-X never
schedules itself). It refuses to run outside the session, logs every run to `data\ingestion.log` and keeps each JSON
report in `data\ingestion_reports\`. It never creates, edits or back-dates candles. After the session:
```powershell
cd backend; ..\.venv\Scripts\python.exe -m app.ops.cli freshness --min-run 8    # exit 0 = enough fresh bars
```
Two collected sessions also give the lifecycle exercise a real overnight stale window, so stale detection and
protective-exit recovery run on recorded data (one session: reported NOT_EXERCISED, never simulated).
Remove the task with `Unregister-ScheduledTask -TaskName "AgentX-Intraday-Ingestion" -Confirm:$false`.

## 2. Provision the staging role and database (administrator)
```powershell
psql -h 127.0.0.1 -U postgres -d postgres -f scripts\staging\provision_staging.sql
```
Creates `agentx_staging_app` (LOGIN, NOSUPERUSER, NOCREATEDB, NOCREATEROLE, NOREPLICATION, NOBYPASSRLS; password
prompted, never stored) and `agentx_staging` owned by it; revokes CONNECT from PUBLIC. Idempotent. `agentx_app` is not
given `CREATEDB`. The administrator gives the staging password to the operator out of band; it is never put in `.env`.

## 3. Copy the archive (operator, after the session)
```powershell
$env:AGENTX_STAGING_DB_PASSWORD = Read-Host "staging password"
powershell -ExecutionPolicy Bypass -File scripts\windows\staging_copy.ps1
```
1. Verifies the role has no elevated attributes and that `agentx_staging` is empty.
2. `alembic upgrade head` on `agentx_staging` as `agentx_staging_app` (process-only environment overrides).
3. `pg_dump --data-only` of the 13 archive tables only from `agentx` (read-only). No users, accounts, orders, fills,
   backtests or audit events are copied. Refuses (without altering anything) if an ingestion job references a user.
4. `pg_restore --single-transaction` into the copy.
5. Compares row counts and an MD5 of every copied table's rows between source and copy.
6. Writes `docs\evidence\<date>_staging_copy.json` (no credentials) and fails unless everything matches.

## 4. Cost schedule for staging
`data\staging\cost_schedule.SYNTHETIC_STAGING_ONLY.json` (git-ignored, never committed):
- version `SYNTHETIC_STAGING_ONLY-2026-11-11.1`, SHA-256
  `0c7711e6e0c2009674aabaabb488abc0e7e3982972f45ec00d95e09b5a6d0977`;
- DELIVERY only; effective 2026-09-01 (inclusive) to 2027-01-01 (exclusive);
- every required DELIVERY charge (brokerage, STT both sides, transaction charge, SEBI fee, IPFT, stamp duty, DP charge,
  GST, slippage) with **made-up round placeholder values**, each `source` labelled `SYNTHETIC_STAGING_ONLY`. They are
  not Zerodha, NSE, SEBI, depository, stamp-duty or GST rates and must not be used to judge profitability.

**Guard:** a schedule whose version or description contains `SYNTHETIC_STAGING_ONLY` is refused unless the settings
belong to a staging tool (`staging-validate`, `staging-lifecycle`, `staging-serve`). That needs all three of: a private
in-process flag that only `app.ops.staging.staging_settings` sets (no environment variable, `.env` entry or constructor
argument can set it), a `*_staging` database other than the test database, and a `STAGING-ONLY:` approval reference.
Development start-up with it fails; tests cover each bypass attempt.

## 5. Run the validator twice and the lifecycle exercise
```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\staging_validate.ps1
```
Runs, as `agentx_staging_app`, with the approval reference above:
1. `freshness --min-run 8` on the copy → `docs\evidence\<date>_staging_freshness.json`;
2. `staging-validate` without participation → `<date>_staging_validate_base.json`;
3. `staging-validate --participation 0.1` → `<date>_staging_validate_participation_0.1.json`;
4. only if both passed: `staging-lifecycle` → `<date>_staging_lifecycle.json`;
5. a summary with every exit code, the schedule file name and SHA-256 → `<date>_staging_validate_summary.json`.

| Exit code (validator and lifecycle) | Meaning |
|---|---|
| 0 | **Passed:** no check failed and the order flow was exercised |
| 1 | at least one check failed |
| 2 | nothing failed, but no fresh scenario existed, so the order flow was not exercised (**not a pass**) |

### Time model (no lookahead)
- A bar is usable only once it has ended **and** been recorded: `max(bar_end, observed_at)`. The simulated clock is
  never set to a bar's close when its recording is later.
- The reference is the newest bar already recorded, at most 15 minutes old.
- A MARKET order accepted at *t* fills on the bar starting at the first 5-minute boundary at or after *t*, in the same
  session. Bars are recorded after they close, so this is usually **not** the chronologically next bar after the
  reference. A missing or ineligible fill bar is never replaced by a later one.
- Processing runs only once the fill bar is recorded; a run one second earlier must leave the order untouched.

### Validator checks
Archive (counts, all `UNVERIFIED_PROVIDER_NATIVE`, final-only, recording-lag distribution); configuration valid on the
scenario date; submission `PENDING` on the expected reference bar; approval reference on the order; idempotent
duplicate; stop required; oversized entry resized or rejected; no fill before the fill bar is recorded; fill on the
expected bar at its open plus slippage with full provenance and the order's approval reference; fees recomputed
independently; ledger and cash reconcile; restart books nothing twice; exit on its expected bar; daily P&L; stale data
rejected; archive unchanged. Not exercised on the copy: partial fills without `--participation`; frozen-position
recovery (no split in the window; covered by integration tests).

### Lifecycle checks (`staging-lifecycle`, through the HTTP API in-process)
Exchange status enabled in simulation only with the approval reference; stop-loss required; oversized entry resized or
rejected and a resized order's reservation released on cancel; valid DELIVERY buy accepted; approval reference;
decision on the expected reference bar; cash reserved; idempotent replay; 409 for a reused `client_order_id`; fill on
the expected bar at open plus synthetic slippage; fees equal to an independent recomputation from the schedule file;
reservation released; position quantity, cash, unrealized P&L and ledger reconciled; kill switch blocks entries; stale
prices block new orders; on a real stale window, protective-exit evaluation records an unresolved requirement, flags
the position unprotected and raises an alert, then resolves on fresh data; exit fill and fees; realized P&L, final cash
and ledger reconciled; no duplicate fills; no open requirement left.

For an interactive session instead, `python -m app.ops.cli staging-serve --staging-db agentx_staging --schedule ...
--approval-reference ... [--replay-from <tz-aware start>]` serves the API on 127.0.0.1:8020 with the same settings.

## 6. Clean up (administrator, after the evidence is saved)
```powershell
psql -h 127.0.0.1 -U postgres -d postgres -f scripts\staging\teardown_staging.sql
```
Delete `data\staging\agentx_archive_*.dump`. The validator's records are append-only, so a staging database is dropped
rather than reused. `agentx` and `agentx_test` are never touched.

## Tests
`tests/unit/test_staging_planner.py` (planner and freshness, realistic lags) and
`tests/integration/test_staging_readiness.py` (exchange timing, approval references, schedule guard, stale protective
exits, lifecycle runner over one and two sessions, backfilled archive) and the runner tests in
`tests/integration/test_phase4_operations.py`.
