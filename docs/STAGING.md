# Staging validation (B9)

Validates the Phase 4 business flow against a **copy** of the real Yahoo archive. The original database is only read
(`pg_dump`). The validator runs only against a database whose name ends with `_staging` and differs from `DB_NAME` and
`DB_TEST_NAME`. Inside the staging copy it creates one validator user and that user's records. It never writes market
data.

**Approval required.** The validator enables `simulation_provider_native` for its own process only, and only with the
approval reference and cost schedule you pass to it. Run it only with an owner-approved staging configuration.
Real-money execution does not exist.

## 1. Create the staging copy (read-only on the source)
```bash
# source = the database holding the real archive (Windows integration machine: agentx)
pg_dump -h 127.0.0.1 -U agentx_app -d agentx -Fc \
  -t data_providers -t instruments -t instrument_provider_symbols -t provider_capabilities -t provider_health_state \
  -t data_ingestion_jobs -t data_ingestion_batches -t data_quality_issues -t data_restatement_events \
  -t exchange_calendar_days -t candles -t adjusted_candles -t corporate_actions \
  --data-only -f agentx_archive.dump
createdb -h 127.0.0.1 -U <admin> -O agentx_app agentx_staging
DB_NAME=agentx_staging python -m alembic upgrade head          # schema at head on the empty copy
pg_restore -h 127.0.0.1 -U agentx_app -d agentx_staging --data-only agentx_archive.dump
```
On Windows, `$env:DB_NAME="agentx_staging"` replaces the `DB_NAME=` prefix. No superuser is needed.

**Verified on 2026-11-11 (macOS):** this procedure copied the local real Yahoo archive (28,577 candles, 10
instruments) into `agentx_staging`. Counts were identical, and the source was unchanged.

## 2. Run the validator
```bash
python -m app.ops.cli staging-validate --staging-db agentx_staging \
  --schedule <approved or clearly-labelled synthetic schedule.json> \
  --approval-reference "<owner approval id for staging>" [--participation 0.1]
```
- The output is a JSON report. Each check is `PASS`, `FAIL` or `NOT_EXERCISED` with a reason.
- Exit code 0 means no check failed.

**What it checks:**
- **The archive:** counts, all bars `UNVERIFIED_PROVIDER_NATIVE`, final-only, data-quality and corporate-action summary.
- **The order flow,** on the session with the longest run of contiguous positive-volume bars:
  - submission is `PENDING` (no fill at submission);
  - a duplicate submission is idempotent;
  - an entry without a stop, and an oversized entry, are rejected or resized by risk;
  - a market BUY fills at the **next bar's open** plus slippage, with full provenance;
  - fees match an independent recomputation;
  - the ledger reconciles and cash = initial capital + ledger;
  - a second processing run, simulating a restart, books nothing twice;
  - the exit fills on the next bar;
  - daily P&L equals the fills' realized P&L;
  - stale data is rejected;
  - the archive's candle count is unchanged.
- **Not exercised on the copy (reported as such):**
  - partial fills, unless `--participation` is given;
  - frozen-position recovery. The Yahoo window had no split at the time of writing, so it is covered by the
    integration tests instead.

## 3. Clean up
`dropdb agentx_staging`. The source database is never modified.

## Status
- The runner is **tested** on a synthetic archive (`test_staging_runner_on_a_synthetic_archive`).
- It has **not yet been run on the real archive**. The real archive is on the Windows machine, and the run needs an
  approved staging configuration.
