# Phase 6 — LLM Explanations and Trading Journal

Status: implemented and tested in the working tree on `main` (not committed). **Paper trading only.** Phase 6 adds
understanding and review of trading activity. It adds no trading capability: an LLM explains, it never decides, sizes,
submits, changes or cancels anything, and the deterministic pipeline (strategies, selection, risk, Mock Exchange,
accounting, agent lifecycle) is unchanged and does not import any Phase 6 code.

Agent execution approval (decision D15) and the Phase 4 staging run (B9) remain **separate, outstanding operational
milestones**. Nothing in Phase 6 enables execution, and Phase 6 tests do not validate live or end-to-end paper execution.

## 1. What Phase 6 adds

| Capability | Where | Source of truth |
|---|---|---|
| Explanations of strategy signals, agent decisions, risk decisions, orders, fills, agent events and trading days | `app/explanations` | Stored agent decisions, candles, configuration versions, orders, risk decisions, fills, positions, agent events; `daily_pnl` and `PortfolioReader` for money |
| Optional LLM narrative of those explanations | `app/llm` | The explanation's verified facts only |
| Trading journal: automatic entries, manual notes, tags, full-text search, filters, trade links, reflections | `app/journal` | Links to the authoritative records; amounts read live |
| Journal analytics (AI-assisted insights) | `app/journal/insights.py` | Counts and sums of stored values; an LLM may only reword them |
| API | `api/v1/explanations.py`, `api/v1/journal.py` | — |
| UI | Journal page, Daily Insights page, explanation panel | Backend responses only |

## 2. Architecture

```
api/v1/explanations.py ──> explanations.service ──> explanations.facts ──read──> agent decisions, candles, config versions,
api/v1/journal.py      ──> journal.service/sync     (trades.py episodes)         orders, risk decisions, fills, positions,
                       ──> journal.insights                                       agent events; exchange.portfolio.daily_pnl,
                                     │                                            PortfolioReader (read-only)
                                     └──> explanations.render (pure rule-based text)
                                     └──> llm.narrator ──> llm.providers (OpenAI-compatible HTTP | Anthropic SDK)
writes: journal_entries, journal_tags, journal_entry_tags, journal_reflections, trade_explanations (and audit_events for
unstored reflection drafts) — nothing else.
```

Boundaries, enforced by `tests/phase6/test_boundaries.py`:
- Phase 6 code imports no order pipeline, Mock Exchange service, risk engine or risk service, agent package,
  backtest runner, provider adapter, ingestion job, scheduler, thread, queue or broker code. It never constructs or
  writes orders, fills, positions, ledger entries, risk or agent rows. It never calls `submit`, `cancel`,
  `process_open_orders`, `set_kill_switch`, `save_settings`, `transition` or any other state-changing method.
- `app/llm` sees only settings and enums: no database session and no service.
- No package below the API imports Phase 6, so trading behaviour cannot depend on it. The agent's existing rule (no
  LLM code) is unchanged.
- No mandatory LLM, scheduler or queue dependency: `requirements.txt` is unchanged.
- The Phase 6 routers do not import the trading or agent routers (their clock dependency is their own).

## 3. Explanations

### Layers and grounding

Facts are built read-only (`app/explanations/facts.py`) in separate layers, so that strategy signal, agent decision,
risk decision, order, execution and outcome are never collapsed into one story:

| Subject | Layers | Evidence stored |
|---|---|---|
| `AGENT_DECISION` | session selection (regime, candidates) → strategy signal (reason, stored indicator values, configured parameters, signal bar) → candle → agent decision (outcome, detail, mode at the cycle) → risk decision → order → fills | instrument and symbol, decision, cycle, configuration version and number, strategy and config id, candle id, observation version, provider, price basis, selection decision, order, risk decision, fill ids |
| `ORDER` | agent decision or protective exit → risk decision → order → fills (reference vs executed price, stored slippage rate, fee breakdown, realized P&L) → trade outcome → position now (unrealized only from `PortfolioReader` marks, else the gate's reason) | order, client order id, risk decision, fills, decision, protective-exit requirement, position, price sources (provider:basis:ref) |
| `AGENT_EVENT` | event (type, actor, transition, details) → agent now | event, cycle, instrument |
| `DAILY_SUMMARY` | signals, agent decisions, orders (by status/source), risk rejections and reasons, fills, closed trades, realized P&L (`daily_pnl`), per strategy, per instrument, notable agent events, reflections | decision, order, risk decision, fill and event ids; closing fills; the P&L service |

Indicator values come from the decision's stored `values`. Thresholds are the configuration version's stored candidate
parameters, merged over the strategy's code defaults (as `build_config` does). Money is copied from stored columns or
from `daily_pnl`/`PortfolioReader`. Nothing is recalculated except sums and counts of stored values.

**Orders are not trades.** A closed trade is a position episode: flat, then open, then flat again, replayed from fills
by simulated time (`price_as_of`) and applied corporate-action adjustments (`app/explanations/trades.py`). Its realized
P&L is the sum of the fills' stored realized P&L, plus cash-in-lieu on adjustments. Its fees are the sum of the fills'
stored fees. Outcome labels: WIN, LOSS or BREAKEVEN by the sign of (realized − fees); UNKNOWN for pre-0007 fills;
OPEN while not flat. Daily summaries report closed-trade realized P&L only. Unrealized P&L is a current mark (portfolio),
never part of a session result.

### Rule-based explanation (always present)

`app/explanations/render.py` (generator `EXPLAIN_V1`, pure) fills fixed templates from the facts. It has one template per
strategy reason (all five strategies' entry and exit reasons plus the session square-off), one per agent outcome (every
`AgentDecisionOutcome`), one per notable agent event and one per instrument block. Values are printed as stored, with no
rounding or arithmetic. A test checks that every number in the rule-based text occurs in the facts.

### LLM narrative (optional)

`app/llm/narrator.py`:
1. Prompt: a fixed system prompt (data-only rules, no advice, no person labels, no causation or significance claims) plus
   `<facts>` JSON and the rule-based text, as delimited data. Journal notes are included (as `<untrusted_user_text>`)
   only when `LLM_INCLUDE_JOURNAL_NOTES=true`.
2. Provider call, bounded by `LLM_TIMEOUT_SECONDS`. `LLM_MAX_RETRIES` covers timeouts, 429 and 5xx only. The Anthropic
   SDK retries itself.
3. Validation, where any failure returns `INVALID_OUTPUT` and the narrative is discarded:
   - strict JSON `{"narrative", "caveats"}`, with bounded lengths and control characters removed;
   - **every number must occur in the facts or be one of them rounded** (`UNGROUNDED_NUMBER`);
   - no trading instruction or recommendation (`TRADING_INSTRUCTION`, `RISK_INCREASE`);
   - no URL, no "statistically significant" or "guaranteed" (`OVERCLAIM`), and no labels about the person
     (`PERSONAL_LABEL`).
4. Statuses: `GENERATED`, `DISABLED` (no or misconfigured provider), `FAILED` (error or timeout), `INVALID_OUTPUT`,
   `RATE_LIMITED`, `NOT_REQUESTED`. Every non-GENERATED status keeps the rule-based explanation. A provider exception
   never reaches the caller.
5. Logs record provider, model, status, error code and latency. They never record prompts, notes or keys.

### Storage and idempotency

GET routes return live, unstored previews and never call an LLM. `POST /explanations/generate` stores a
`trade_explanations` row with the facts, evidence, `facts_fingerprint` (SHA-256 of the canonical facts), rule-based text,
narrator (`rule_based` or `provider:model`), status, narrative, caveats, error and latency. The same facts explained by the
same narrator are stored once: the stored row is returned with `200`. Failed attempts are stored as an audit and count
toward the per-user hourly budget (`LLM_MAX_REQUESTS_PER_USER_PER_HOUR`, default 30); beyond it a request is
`RATE_LIMITED`. Unstored reflection drafts are recorded as `audit_events` (`llm.reflection_draft`) so they count too.

## 4. Trading journal

### Automatic entries (no new event pipeline)

Agent-X has no event bus: events are rows. `POST /journal/sync` (default: the last 30 days, at most 366) scans the
window's records and inserts one AUTO entry per record with `INSERT … ON CONFLICT DO NOTHING` on the unique
`(user_id, source_key)` index. Repeated or concurrent syncs create nothing new. Nothing hooks into the execution
pipeline, so the journal can never slow it down or make it fail.

| Source event | Derived from | Key |
|---|---|---|
| ENTRY_SIGNAL / EXIT_SIGNAL | agent decisions (EVALUATION) with ENTER_LONG / EXIT_LONG, whatever the outcome (incl. SIGNALS_ONLY) | `SIGNAL:decision:{id}` |
| RISK_REJECTION / ORDER_REJECTED | rejected orders (by the risk engine / otherwise) | `REJECTED:order:{id}` |
| ORDER_CANCELLED | cancelled orders | `CANCELLED:order:{id}` |
| PARTIAL_FILL | a fill after which its order was still incomplete | `PARTIAL_FILL:fill:{id}` |
| PROTECTIVE_EXIT | Phase 4 `RISK_EXIT` orders | `PROTECTIVE_EXIT:order:{id}` |
| MANUAL_TRADE | manual orders with a fill | `MANUAL_TRADE:order:{id}` |
| TRADE_CLOSED | a position episode returning to flat | `TRADE_CLOSED:fill:{closing fill}` |
| AGENT_INTERVENTION | agent events: error, kill-switch pauses, execution unavailable, reconciliation failure, instrument blocks (MANUAL_INTERVENTION / ORPHANED_OVERNIGHT / DATA_ERROR), expired or abandoned owed exits | `AGENT_EVENT:event:{id}` |
| SESSION_SUMMARY | one per session with activity, once the session has ended (15:30 IST) | `SESSION_SUMMARY:{date}` |

An entry stores identifiers and labels only (`system_facts`, `outcome`), never amounts. The detail view reads the order,
fills, position and trade live from the authoritative tables, so the journal cannot become a second source of truth.

### Manual entries, notes and tags

Manual entries carry: title; instrument; strategy; links to an order, fill, position, agent decision, event,
configuration version or backtest run; entry and exit thesis; market observations; what went well; what went wrong;
lessons; emotional-discipline notes; custom notes; rating (1–5); confidence (1–5); tags; and timestamps.

Links are checked twice: by the service, and by a database trigger. A link to another user's record gets the same `422
LINK_NOT_FOUND` as a missing one, so the response never reveals that the record exists. Inconsistent links (different
instruments, or a fill that is not on the order) get `422 LINKS_INCONSISTENT`. The instrument is derived from the links.

On AUTO entries, only the notes, rating, confidence and tags are editable. The trigger keeps the title, links, system
facts, outcome and times, and refuses to delete the entry. Manual entries are fully editable and deletable; their tag
links and reflection go with them.

Tags are per user, normalised to lower case (letters, digits, `-`, `_`, at most 40 characters), with at most 20 per entry.

### Search and filters

`GET /journal` supports:
- full-text search over the title (weight A) and every note (weight B), using a stored generated `tsvector` with a GIN
  index (built-in PostgreSQL, no extension) and `websearch_to_tsquery('english', q)`;
- filters for instrument, strategy, session date range, outcome, tags (all must match), entry type, source event, order,
  position and has-reflection;
- sorting by occurred, created, updated or relevance;
- `limit` up to 100 and `offset` up to 10 000, with a `total` count.

### Reflections

`POST /journal/{id}/reflection` creates or replaces the reflection: original thesis, whether the setup followed the
strategy rules, whether risk limits were respected, whether the exit followed the strategy, mistakes, lessons and future
improvements. It is allowed only for an entry linked to a **closed** trade; otherwise `409 TRADE_NOT_CLOSED`.

`POST /journal/{id}/reflection/assist` returns neutral, deterministic questions built from the trade's facts, plus an
optional LLM draft of a factual summary. **Nothing is saved**: the user writes and saves the reflection. Questions and
drafts never label the user, and the validator rejects person labels.

## 5. Journal analytics (AI-assisted insights)

`GET /journal/analytics?date_from&date_to` (default 30 days, at most 366) is deterministic. It reports:
- closed trades by strategy, by (strategy, regime at selection), and by instrument, each with sample size `n` and
  win/loss/breakeven counts, a win share in % (with `n`), and summed net realized P&L by strategy;
- exits by source;
- signals versus execution: decisions by outcome, and orders filled, rejected and cancelled;
- suppressed signals and risk rejections by instrument;
- journal usage: entries, tag frequency, yes/no counts of the reflection answers, and frequent words in recorded
  mistakes (labelled "word counts, not a diagnosis").

Caveats are always included: the sample size; "fewer than 30 closed trades: too few to draw conclusions"; no
significance test was performed; correlation, not causation; unverified data, so past results do not predict future
results; and analytics never change strategies, risk or the agent.

`POST /journal/analytics/narrative` stores an optional LLM narrative of these numbers, under the same validation.
Recommendations to raise risk or size are rejected.

## 6. Database (migration `0013`, additive; revises `0012`)

| Table | Purpose | Guards |
|---|---|---|
| `journal_entries` | AUTO and MANUAL entries, links, system facts, notes, `search_vector` (generated) | unique (user, source_key); AUTO system columns immutable and AUTO rows undeletable; identity immutable; every link must belong to the user (trigger); title not blank; rating/confidence 1–5 |
| `journal_tags` | per-user tags | unique (user, name); normalised name |
| `journal_entry_tags` | entry ↔ tag | PK (entry, tag); entry and tag of the row's user (trigger) |
| `journal_reflections` | one per entry | unique entry; the entry's user (trigger) |
| `trade_explanations` | stored explanations | append-only; decision/order/event of the row's user (trigger); unique successful (user, subject, facts fingerprint, narrator); `narrative IS NOT NULL` iff GENERATED |

All five tables are user-owned (`UserOwnedMixin`) and classified in `test_every_table_is_classified`; FKs are `RESTRICT`.

Retention: explanations and AUTO entries are kept, and nothing deletes them. A MANUAL entry, its tag links and its
reflection are deleted together on the user's request. Downgrading `0013` is refused while any journal or explanation
row exists. **The development database has not been migrated** (it is still at `0012`); migrating it is an owner action
(§9).

## 7. API

See [API.md](API.md) §Phase 6. All routes require authentication, read the user only from the JWT, return `404` for
another user's records, bound pagination, refuse unknown body fields, and return `{"detail": "CODE: message"}` errors
without internal details.

## 8. Security and privacy

- **Isolation:** `owned_select` everywhere, plus ownership triggers on every link. Cross-user access is tested for every
  route.
- **Untrusted text:** notes and any external content are sent to an LLM only when explicitly enabled, inside a
  data-only block. Whatever they contain, the output validator refuses instructions, recommendations, URLs and
  ungrounded numbers. The narrator has no tools and no database, so text cannot invoke anything.
- **Data minimisation:** prompts contain only trading facts (symbols, prices, ids). There is no email, account id or
  token. Keys are `SecretStr`, never logged and never returned (`/explanations/provider` shows the provider, model and
  configuration problems only).
- **Rendering:** the UI renders all text as React text nodes (never as HTML). The AI narrative is visually separate and
  labelled "not a verified trading fact, not advice and not an instruction".
- **Bounds:** text fields are at most 10 000 characters, titles at most 200, at most 20 tags, queries at most 200
  characters, windows at most 366 days, provider timeouts at most 120 s with at most 3 retries, and a per-user hourly
  LLM budget.
- **Rate limiting** beyond the LLM budget does not exist yet (a Phase 8 item, as before).

## 9. Setup and operations

1. Without any LLM: nothing to configure. `LLM_PROVIDER=disabled` is the default; every explanation is rule-based.
2. Local, free LLM (example: Ollama): run the server, then set `LLM_PROVIDER=openai_compatible`,
   `LLM_BASE_URL=http://127.0.0.1:11434/v1` and `LLM_MODEL=<a pulled model>`.
3. Claude: `pip install anthropic`, then set `LLM_PROVIDER=anthropic` and `LLM_API_KEY=...`. `LLM_MODEL` defaults to
   `claude-opus-5-5`. For current models the request uses low effort and the server-side refusal fallback
   (`fallbacks: "default"`). A refusal is reported as `FAILED/REFUSED`, and the rule-based text stays.
4. Owner action: migrate the development database to `0013` after a backup (`alembic upgrade head`), as for `0012`.
5. Daily use: open Trade Journal (it syncs the last 30 days) and Daily Insights.

## 10. Tests

| File | Covers |
|---|---|
| `tests/unit/test_llm_narrator.py` | disabled by default; missing key; misconfiguration; success; timeout and retry bound; non-retryable errors; provider exceptions; 11 kinds of invalid output; number grounding with rounding; notes only when enabled; injected instructions refused; the OpenAI-compatible adapter over loopback HTTP (success, 500 retries, unreachable); no key in status |
| `tests/phase6/test_render.py` | every strategy reason explained from its values and parameters with grounded numbers; every agent outcome has a sentence; layers stay separate |
| `tests/phase6/test_boundaries.py` | the architecture rules above |
| `tests/integration/test_phase6_records.py` | real records (agent ORB round trip, partial fill, cancellation, risk rejection, SIGNALS_ONLY, late signal): closed-trade reconstruction from stored values; signal, order and daily explanations grounded in the facts; daily P&L equals `daily_pnl`; idempotent sync; no AUTO session summary before 15:30; immutability and ownership triggers; manual links and reflections; generation idempotency; fallback; hourly budget; generation leaves every trading table unchanged |
| `tests/integration/test_phase6_api.py` | 401 on all 20 route/method pairs; sync, search, FTS, tags, dates, outcome, pagination, order filter; AUTO facts immutable and undeletable; manual lifecycle and validation (blank title, rating, unknown fields, sizes, tags, links); reflections; assist never saves; full cross-user isolation; preview, generate, idempotency, provider, timeout and invalid-output fallback; daily summary and analytics; window bounds |

Verification results are in §12.

## 11. Known limitations

- Automatic entries appear when `POST /journal/sync` runs (the Journal page runs it on open). There is no push or
  scheduler, by design (no background work).
- Generation is synchronous within the request, bounded by timeout × (retries + 1). There is no job queue.
- A trade is a position episode per instrument. Phase 4 keeps one position per (user, instrument), so overlapping
  manual and agent activity in one instrument forms a single episode.
- The regime used in analytics is the agent's session selection. Trades without an agent selection show `UNKNOWN`.
- English full-text stemming (e.g. "patience" and "patient" stem differently). Notes in other languages are matched
  only on exact words.
- Number grounding accepts any number that occurs in the facts, so a model could in principle repeat a true figure in
  a misleading sentence. The UI therefore keeps the rule-based explanation primary and labels the narrative.
- The per-user LLM budget is the only rate limit. General API rate limiting remains a Phase 8 item.
- The frontend keeps the access token in this tab's sessionStorage (as the Phase 5 panel does). There is no refresh
  token.

## 12. Verification

2026-11-12, Windows, Python 3.14, PostgreSQL test database `agentx_test` only. The development (`agentx`, still at
`0012`) and staging databases were not touched, and execution stayed disabled.

- **Baseline before Phase 6:** 1525 tests collected. 1524 passed and 1 failed. The failure was the migration
  round-trip test, caused by the new migration file appearing on disk mid-run; it passes with the Phase 6 models.
- **Full suite with Phase 6** (run with `-W default`): **1615 passed, 0 failed, 0 skipped, in 25m17s**, with 4
  warnings:
  - the existing `httpx` TestClient deprecation;
  - 3 `ResourceWarning`s from the new loopback test. These were then fixed: the HTTP error is closed and the test
    server's socket is closed.
- **After the final fixes** (cancelled-entry dating, wording, warnings): all Phase 6 and boundary tests, the migration
  round trip and isolation were re-run, and 115, then 64, passed with only the `httpx` warning.
- **Migration:** `0001 → 0013 → base → head` round trip with no model drift
  (`test_migrations_round_trip_and_match_the_models`).
- **Mutation checks:** 4 of 4 caught. The 4 safeguards disabled were number grounding, the output validator, the import
  boundary, and notes-only-when-enabled.
- **Frontend:**
  - `npm run build` (type check and production build) passes.
  - Headless Chrome was driven against the Vite dev server and the backend on `agentx_test`, seeded with the Phase 5
    ORB round trip.
  - The Journal page synced 5 entries (and created 0 on a repeat), and showed the closed trade with the stored figures
    (gross −78.419250, fees 116.799988, net −195.219238), the layered decision explanation and the reflection form.
  - Daily Insights showed the session summary and analytics caveats.
  - No console errors from application code.
- **Not demonstrated:** a real LLM provider call (providers were exercised with a fake provider and a loopback
  OpenAI-compatible server; the Anthropic SDK is not installed here). Live or end-to-end paper execution was also not
  demonstrated: D15 and B9 are outstanding.
