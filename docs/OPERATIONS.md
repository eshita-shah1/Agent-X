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

Windows (Task Scheduler, PowerShell):
```powershell
$action  = New-ScheduledTaskAction -Execute "C:\path\Agent-X\.venv\Scripts\python.exe" `
           -Argument "-m app.ops.cli daily --report-dir ..\data\ingestion_reports" -WorkingDirectory "C:\path\Agent-X\backend"
$trigger = New-ScheduledTaskTrigger -Once -At 09:20 -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Hours 6 -Minutes 20)
Register-ScheduledTask -TaskName "AgentX-Ingestion" -Action $action -Trigger $trigger
```
- The cadence is an operational decision (§2).
- Overlapping runs are refused (exit 3), so a short interval is safe.

## 2. Latency and the staleness threshold (B7)

`EXECUTION_MAX_PRICE_AGE_SECONDS` (provisional 900 s) is the maximum age of the reference bar at decision time.
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
