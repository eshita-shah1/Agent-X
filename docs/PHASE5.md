# Phase 5 — Autonomous Agent Orchestration

Status: implemented and tested on branch `phase-5-agent` (not committed). **Paper trading only**: no broker, no real
money, no LLM. Execution stays **disabled in development**; there, the agent runs `SIGNALS_ONLY` and never submits an
order. End-to-end paper execution needs an approved execution environment (decision D15, deferred), so everything
below that involves fills is proven with synthetic fixtures in the test suite only (§9).

## 1. What the agent does

```
External scheduler (Task Scheduler / cron)             operator-registered; Agent-X never schedules itself (D8)
  └─ python -m app.agent.cli cycle                     one-shot: every configured agent once, then exit
       per agent, under a PostgreSQL advisory lock (one worker per agent at a time):
       T0  record the cycle (a cycle left RUNNING by a dead worker becomes INTERRUPTED)
       T1  Phase 4 duties: process resting orders, refresh risk, run protective exits      own commit
       T2  account lock -> agent lock -> status/kill switch/execution checks -> reconcile ->
           per instrument: replay archive (MarketDataService) -> approved selector + strategies ->
           decisions -> OrderService.submit -> Phase 4 risk engine -> Mock Exchange                 one commit
```

- **Reused, never duplicated:** `MarketDataService.get_candles` (bars); `ExecutionPriceGate.check` (freshness,
  provenance, data quality, corporate-action bar rules); `app.strategies` and `app.strategies.selection` (the five
  approved strategies, regime classification, PINNED and REGIME_PRIORITY, session lock); `OrderService.submit`;
  `MockExchange.process_open_orders`; `RiskService.refresh` and `set_kill_switch`; `OrderService.run_protective_exits`.
- **Never done by the agent:** writing orders, fills, positions, ledger or risk rows; sizing for risk; computing costs or
  P&L; reading backtest results; contacting a provider; triggering ingestion. Boundary tests enforce each rule
  (`tests/agent/test_boundaries.py`).
- **Fresh data:** only the operator-scheduled Phase 2 ingestion archives bars (`AgentX-Intraday-Ingestion`). The agent
  task runs two minutes after it (`AgentX-Agent-Cycle`). If ingestion stops, prices go stale, the gate refuses entries,
  and Phase 4 records unresolved protective exits.

## 2. Approved decisions implemented

| # | Decision | Implementation |
|---|---|---|
| D2 | An agent position that outlives its session is `ORPHANED_OVERNIGHT`; no automatic carry-over square-off | The instrument is blocked for the agent; only Phase 4 stops and targets and the user manage the position. An owed exit expires at its session's end (`agent.owed_exit_expired_session_end`) |
| D3 | Stop and target distances anchored to the signal bar's close | `pipeline.protective_prices`: an absolute strategy stop (ORB) is used as given; distances are taken from the archived close (6 dp, half-even) |
| D4 | The agent proposes an upper-bound quantity; the risk engine resizes it | `max_position_quantity`, else whole shares that available cash covers at the reference price, in whole lots. EXECUTION mode requires `resize_allowed = true` |
| D5 | Late entry signals are never acted on | An entry is submitted only when its bar is the gate's latest eligible bar; otherwise `SUPPRESSED_LATE_SIGNAL` |
| D6 | Owed exits persist until the position is flat | A strategy exit (or 15:15 square-off) sets an owed exit; a MARKET SELL is submitted on each new eligible bar until flat, including after a kill-switch cancellation. It persists within the session, also for an instrument dropped by a newer configuration version; D2 ends it at the session boundary (D2 takes precedence over D6 there) |
| D7 | Manual intervention blocks further agent orders in that instrument | `MANUAL_INTERVENTION`: an open manual order, a manual fill this session, a position with a manual fill or no agent entry, or an applied corporate-action adjustment. It lasts for the rest of the session |
| D8 | One-shot cycles through an external scheduler | `app/agent/cli.py`, `scripts/windows/agent_cycle.ps1`, `register_agent_task.ps1` |
| D9 | SIGNALS_ONLY when execution is disabled | Decided at start from the same readiness function as `GET /exchange/status`. Signals are recorded as `SIGNALS_ONLY_NOT_SUBMITTED`; no AGENT order is ever submitted (tested by making `OrderService.submit` raise) |
| D10 | STOP keeps Phase 4 protective-exit monitoring | The worker runs T1 for every configured agent, whatever its status |
| D11 | Reuse the Phase 4 kill switch; releasing it does not resume | `POST /agent/kill-switch` calls `RiskService.set_kill_switch` and pauses the agent. A switch engaged through `/risk/kill-switch` pauses it on the next cycle. Resume is refused while the switch is engaged |
| D12 | Versioned configuration, changed only when not RUNNING, effective from the next session | `agent_config_versions` (append-only). Before the first start a new version applies immediately |
| D13 | Shared configuration and warm-up helpers | `app/strategies/configs.py` (`build_config`, `build_policy`, `lookback_days`, `corporate_action_floor`, `warmup_sessions_before`); the backtest re-exports and wraps them unchanged |
| D14 | The guard test allows `app/agent` only | `test_phase2_policies.py`: `agent` was removed from the forbidden list; the scheduler/thread guard and every other boundary are unchanged |
| D16 | The backtest's warm-up default | `warmup_sessions = 2` |

## 3. Lifecycle: STOP vs PAUSE vs KILL SWITCH

| | Strategy evaluation | New entries | Strategy / owed exits | Phase 4 protective exits | Open orders | Positions |
|---|---|---|---|---|---|---|
| RUNNING | yes | yes (EXECUTION mode) | yes | yes | processed | kept |
| PAUSED (monitor mode) | yes | **suppressed** (recorded) | yes | yes | processed | kept |
| STOPPED | **no** | no | no; owed exits are abandoned and recorded | **yes** (D10) | processed | **kept, never liquidated** |
| ERROR | no | no | no | yes | processed | kept |
| Kill switch (Phase 4) | while RUNNING it **pauses** the agent | blocked by Phase 4 | yes (exits reduce risk) | yes | **all cancelled** (Phase 4 behaviour) | kept, never liquidated |

Transitions (the `agent_instances` trigger enforces the same table): `STOPPED→RUNNING` (start),
`RUNNING→PAUSED|STOPPED|ERROR`, `PAUSED→RUNNING|STOPPED|ERROR`, `ERROR→RUNNING|STOPPED`.

- **Start** validates the effective configuration, the instruments (enabled equities only, so futures are refused), the
  market-data provider's registration, execution readiness (deciding the mode), risk settings and `resize_allowed` in EXECUTION mode, and that the kill switch
  is released. It then reconciles and moves to RUNNING. The agent acts only on bars that finalize after it starts.
- **Resume** (from PAUSED or ERROR) repeats the validation in the **same mode** as at start (an EXECUTION agent cannot
  resume while execution is unavailable; stop and start it to run SIGNALS_ONLY), then reconciles. Signals from the
  paused period are late and are never acted on.
- **Automatic transitions** (by the worker, recorded): the kill switch engaged → PAUSED; execution unavailable for an
  EXECUTION agent → PAUSED; a reconciliation discrepancy → ERROR; `AGENT_MAX_CONSECUTIVE_FAILURES` (3) failed cycles →
  ERROR.

## 4. Reconciliation (start, resume, every cycle)

1. Process resting orders and refresh risk (Phase 4).
2. **Discrepancies:** an AGENT order without an agent decision; a decision whose order has another client order id; an
   AGENT order from an earlier session still open after processing; a non-agent order using the agent's `AG:` client-id
   prefix. Any of these refuses start or resume, or puts a running agent in ERROR. No compensating order is ever created.
3. **Instrument blocks** are derived from orders, fills, positions and adjustments: `MANUAL_INTERVENTION`,
   `ORPHANED_OVERNIGHT`, `DATA_ERROR` (the archive could not be replayed, e.g. the instrument became disabled; this is
   contained to that instrument).
4. **Strategy position context** is rebuilt from fills (`fills.price_as_of`) for every bar of the session, never held in
   memory. Partial fills keep the position open; rejected or cancelled entries leave the strategy flat.

## 5. Determinism, idempotency and restart safety

- **Replay:** each cycle rebuilds the selector and all candidate strategies from the archive. Warm-up sessions are fed
  FLAT, as in backtest warm-up; the anchor (first warm-up session) is fixed for the session; the warm-up never crosses a
  split/bonus ex-date. Then today's finalized bars that had ended **and been observed** by the cycle's clock are fed.
  Forming candles and future observations are never used.
- **Watermark:** each instrument records the last processed bar, and only later bars produce decisions. One EVALUATION
  per (agent, instrument, bar) and one SELECTION per (agent, instrument, session) are also database rules.
- **Client order ids:** `AG:E:{instrument}:{config_version}:{signal_bar_epoch}` and
  `AG:X:{instrument}:{position_opened_epoch}:{eligible_bar_epoch}`; at most one decision per client order id
  (database). Phase 4 idempotency (`client_order_id` + fingerprint) is unchanged.
- **Crash during a cycle:** T2 is a single transaction, so an order accepted by OrderService but not yet journaled
  rolls back with it, and the retry submits exactly once (tested). A RUNNING cycle row left by a dead worker becomes
  INTERRUPTED.
- **Duplicate workers:** a session-level `pg_try_advisory_lock` per agent. The second worker reports `SKIPPED_LOCKED`;
  a crashed worker's lock disappears with its connection.
- **Lock order:** account row, then agent row, in both the API and the worker. A pause therefore waits for an
  in-flight cycle, and no submission happens after a committed pause.

## 6. Market data and freshness

The agent uses `MarketDataService` only. It preserves provider, price basis (`UNVERIFIED_PROVIDER_NATIVE`), candle id
and observation version on every decision, and never relabels data. Entry eligibility is the Phase 4 gate's judgement
(900 s maximum age, final, canonical, data quality, corporate-action rules). Missing bars are handled by the strategies'
own gap rule (`GAP_SUPPRESSED`). Yahoo's delay is not assumed.

**Timing risk (disclosed):** with a 5-minute ingestion cadence the 15:15 square-off is typically accepted about
15:20–15:25 and fills on the session's last bars. One more interval of lag cancels it (`NEXT_BAR_UNAVAILABLE`) and the
position becomes `ORPHANED_OVERNIGHT` (D2).

## 7. Database (migration `0012`, additive; revises `0011`)

| Table | Purpose | Guards |
|---|---|---|
| `agent_instances` | lifecycle, mode, heartbeat | one per user; never deleted; identity immutable; transition trigger |
| `agent_config_versions` | immutable configurations | append-only; unique (agent, version) |
| `agent_cycles` | one row per cycle | one RUNNING per agent; finished rows frozen; never deleted |
| `agent_decisions` | the deterministic journal | append-only; one evaluation per bar; one selection per session; one row per client order id; references only its own user's order and risk decision |
| `agent_events` | transitions, kill switch, reconciliation, failures | append-only |
| `agent_instrument_states` | watermark, replay anchor, owed exit, block | one per (agent, instrument); never deleted |

All six tables are user-owned (`UserOwnedMixin`, `owned_select`, classified in `test_every_table_is_classified`). An
insert trigger requires each row's `user_id` to equal its agent's. Downgrade is refused while any agent record exists.
Phase 4 tables are untouched. **The development database has not been migrated** (still `0011`); migrating it is an
owner action (§10).

## 8. API (`/api/v1/agent`; details in [API.md](API.md))

`GET status`, `GET config`, `PUT config`, `POST start | pause | resume | stop | kill-switch`,
`GET decisions | events | cycles`. Another user's agent is never visible (404, empty lists). Transitions return the
persisted state read back after commit. `PUT /api/v1/risk/settings` now returns **409 `AGENT_RUNNING`** while the
agent is RUNNING (pause or stop it first).

## 9. Verification

- **Full suite (2026-11-12, Windows, PostgreSQL, Python 3.14):** 1525 passed (baseline before Phase 5: 1455). `alembic`
  round trip 0001→0012→base→head with no model drift (`test_migrations_round_trip_and_match_the_models`). Frontend
  `npm run build` (type-check + build) passes.
- **Agent tests:**
  - `tests/agent/test_pure.py`: keys, D3/D4 mapping, position context, transitions, fingerprints, next-session rule, the
    shared warm-up rule, and replay equivalence for **all five strategies** (identical signals to direct evaluation, on
    data that produces real entries).
  - `tests/agent/test_boundaries.py`: the architecture rules.
  - `tests/integration/test_agent_cycle.py`: a full ORB paper lifecycle (entry → risk-sized MARKET BUY → next-bar fill →
    15:15 square-off → owed exit → SELL fill → flat), idempotent repeats, late/stale/forming/missing data, risk
    rejection, SIGNALS_ONLY, execution becoming unavailable, pause, kill switch (cancel, pause, no auto-resume),
    external kill switch, manual intervention, overnight orphan without carry-over, duplicate worker, interrupted cycle,
    crash after submission, repeated failures → ERROR → resume, Phase 4 duties surviving orchestration failures,
    discrepancy → ERROR, STOP without liquidation but with protective exits, a disabled instrument contained, database
    tamper guards, and every strategy through a database cycle.
  - `tests/integration/test_agent_api.py`: authentication, configuration versions and validation, SIGNALS_ONLY
    disclosure, transitions, risk-settings and configuration locks, kill switch, isolation, ignored client user ids,
    one agent per account.
- **Mutation checks:** 10 safeguards disabled one at a time (watermark, SIGNALS_ONLY, late signal, pause suppression,
  risk-settings lock, manual block, no carry-over, kill-switch resume refusal, discrepancy detection, T1 isolation):
  10 of 10 caught.
- **Not demonstrated:** a full autonomous paper-trading session under an approved execution policy (D15 deferred; Phase 4
  staging B9 is a separate milestone and has not run).

## 10. Operations

See [OPERATIONS.md](OPERATIONS.md) §7. In short:
1. (Owner) migrate the database: `alembic upgrade head` (to `0012`) after a backup.
2. Configure and start the agent (API or the dashboard's Agent control panel).
3. (Owner) register `AgentX-Agent-Cycle` (`scripts\windows\register_agent_task.ps1`) next to the ingestion task.
4. Watch `GET /api/v1/agent/status` (`WORKER_NOT_SEEN`, blockers, instrument blocks), `data\agent.log`, and
   `python -m app.agent.cli status`.

## 11. Known limitations

- One policy (universe, mode, candidates) for all of an account's instruments, and one agent per account.
- Entries are MARKET orders only, and the exit for a whole position is one MARKET SELL.
- Entries fill one to two bars later than in the Phase 3 backtest (ingestion latency plus next-bar-open); the two
  models differ by decision (BACKTESTING.md §9).
- STOP abandons owed exits (recorded); open positions then rely on their Phase 4 stop-loss/take-profit only.
- In SIGNALS_ONLY mode the agent never submits; Phase 4 protective exits (source `RISK_EXIT`) still run for the
  account, as they do through `POST /exchange/protective-exits`. While execution is disabled they cannot submit
  anything either.
- `WORKER_NOT_SEEN` is informational; the platform does not supervise the scheduler.
- The frontend panel is the only real-data part of the dashboard; the chart and trade table remain labelled demo data.
