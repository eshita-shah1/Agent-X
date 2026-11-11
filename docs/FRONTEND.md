# Frontend

The `frontend/` directory holds the Agent-X web UI. It was imported unchanged from the standalone frontend project. The only integration changes are a development proxy in `vite.config.ts`, `frontend/.env.example` and `frontend/.gitignore`.

**Status: the UI runs entirely on mock data.** It does not call the backend yet. Every number shown is generated in the browser (`src/data/mock*.ts`, `src/lib/marketData.ts`), including prices, trades, portfolio, risk, backtests and benchmarks. None of it is market data, a simulation result or a backtest result.

## Stack and commands

| | |
|---|---|
| Framework | React 18, TypeScript 5, Vite 5 |
| Libraries | Tailwind CSS 3 (shadcn-style Radix UI components), React Router 6, TanStack Query 5, Zustand 5, react-hook-form + zod, Recharts 2, lightweight-charts 4 |
| Package manager | npm (`package-lock.json` is authoritative; use `npm ci`) |
| Node | Verified with Node 20.18 / npm 10.8 |

```bash
cd frontend
npm ci            # install exactly what the lockfile pins
npm run dev       # http://localhost:5173
npm run build     # type-check (tsc -b) and production build into frontend/dist (git-ignored)
npm run preview   # serve the production build
```

The `lint` script (`tsc --noEmit`) checks nothing as configured, because the root `tsconfig.json` has `files: []` and only references the app and node configs. The real type-check is `tsc -b`, which runs inside `npm run build`. There is no frontend test suite or ESLint config yet.

## Connecting to the backend

- **No CORS on the backend.** The FastAPI app has no CORS middleware. Rather than change backend policy, the Vite dev server proxies `/api` and `/health` to the backend, so the browser stays same-origin.
- **Proxy target.** Set `VITE_API_PROXY_TARGET` in `frontend/.env.local` (copy `frontend/.env.example`). It defaults to `http://localhost:8000`.
- **Production.** Serve the built UI and the API from the same origin through a reverse proxy, or add an explicit CORS allow-list to the backend. Either is a Phase 8 deployment decision.
- **Base path.** API calls should use the relative base `/api/v1`.
- **Authentication.** `POST /api/v1/auth/login` returns a JWT bearer access token. There are no refresh tokens. Every user, trading and backtest endpoint needs `Authorization: Bearer <token>`. The UI currently has **no login screen** and no token handling.

Two runs were verified on 2026-11-12:
- Through the dev proxy, `/health` returned `ok`, `/health/ready` returned `ready`, and `/api/v1/auth/me` without a token returned 401.
- The production build rendered every route headlessly with no console errors.

## Integration gaps (page by page)

These gaps are documented, not worked around. No backend endpoint was added or changed for the UI.

| Page / feature | Frontend today | Backend today | Gap |
|---|---|---|---|
| Login / session | none | `auth/register`, `auth/login`, `auth/me` | The UI needs a login flow and token storage. |
| Instruments panel, chart | Mock list including NIFTY 50 / BANKNIFTY **indices** with last price, change and volume | `GET /market/instruments`: 10 NSE equities; NIFTY/BANKNIFTY futures disabled; no index instruments, no quote fields | No index data. No last price or day change endpoint; it can be derived from the latest candles. |
| Candlestick chart | Generated bars for 1m / 5m / 15m / 1H / 1D | `GET /market/candles`: 5-minute archive, `UNVERIFIED_PROVIDER_NATIVE` | Only 5m is archived; other timeframes would need aggregation (not present). The UI should show the price-basis label. |
| Trading: order ticket | Buy/Sell; Market, Limit, **Stop Loss, Stop Limit**; long and **short** | `POST /orders`: BUY/SELL, MARKET/LIMIT only, long-only, DELIVERY product, required stop-loss price, `client_order_id`; fills only after `POST /exchange/process` at the **next bar's open**; execution **disabled** by default | Stop order types and shorts do not exist. Orders are accepted as PENDING, never filled instantly. With execution disabled, they are not filled at all. |
| Positions / portfolio | Mock holdings and equity curve by 1D…All | `GET /portfolio`, `/portfolio/fills`, `/portfolio/pnl/daily`, `GET /account` | No equity-curve time-series endpoint (daily P&L exists). |
| Dashboard: trade history, agent status | Mock trades, agent "running/halted" toggle | Orders and fills exist; **no agent** (Phase 5) | The agent status and start/stop controls have no backend. |
| Risk management | Mock limits, exposure, streaks, alerts | `GET/PUT /risk/settings`, `GET /risk/state`, `POST /risk/kill-switch`, `GET /risk/decisions` | Field names and limits differ; it needs mapping. Exposure history, streak and scatter analytics have no endpoint. |
| Backtesting lab | Single strategy name, symbol, date-range label, stop/target/slippage %; synchronous result | `POST /backtests`: `instrument_ids`, `start`/`end` dates, `mode` (REGIME_PRIORITY / PINNED), `candidates` with `strategy_id`/params, cost model in bps; runs **asynchronously**; results via `/backtests/{id}/...` | Different request shape, and the UI needs to poll for results. Strategies come from `GET /backtests/strategies`. About 60 days of 5m data limits ranges. |
| Benchmark | Mock strategy vs Buy-and-Hold comparison and metrics | The Buy-and-Hold benchmark is computed inside backtest runs | No standalone benchmark endpoint; read it from a backtest run. |
| Journal and trade assistant chat | Mock reports and a scripted chat | None (LLM journal is Phase 7) | No backend. The LLM may only explain decisions, never make them. |
| Settings | Local Zustand state | Risk settings only | Workspace preferences are UI-local. |

## Recommended order of integration (not done)

1. Add an API client and a login screen.
2. Read-only market data (instruments and 5m candles, with the unverified label).
3. Account, portfolio and risk state.
4. Backtest runs (async).
5. Order submission, shown as PENDING with execution disabled.

Keep the mock modules only behind an explicit "demo data" switch, so that mock numbers are never presented as real.
