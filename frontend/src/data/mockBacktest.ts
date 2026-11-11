import type { BacktestConfig, BacktestResult, BacktestSeriesPoint, PeriodPnlPoint } from '@/types/trading';

const BASE_DATES = [
  'Mar 01', 'Mar 10', 'Mar 20', 'Mar 31',
  'Apr 10', 'Apr 20', 'Apr 30',
  'May 10', 'May 20', 'May 31',
  'Jun 10', 'Jun 20', 'Jun 30',
  'Jul 10', 'Jul 20', 'Jul 31',
  'Aug 10', 'Aug 20', 'Aug 31',
  'Sep 10', 'Sep 20', 'Sep 30',
  'Oct 10', 'Oct 20', 'Oct 31',
  'Nov 10', 'Nov 20', 'Nov 30',
];

const STRATEGY_RETURNS_DEFAULT = [
  0.0, 0.7, 1.3, 1.6,
  1.1, 1.4, 0.5,
  1.7, 2.9, 4.4,
  5.1, 6.2, 7.6,
  8.9, 10.6, 10.1,
  9.3, 10.2, 11.4,
  12.8, 14.5, 16.4,
  18.1, 19.8, 21.5,
  22.8, 23.9, 24.86,
];

const BUYHOLD_RETURNS_DEFAULT = [
  0.0, 0.9, 1.8, 2.5,
  2.2, 1.9, 2.4,
  3.8, 5.4, 7.2,
  8.1, 7.7, 7.1,
  8.4, 10.1, 11.8,
  12.6, 12.1, 11.9,
  13.2, 14.3, 15.1,
  15.9, 16.4, 16.8,
  17.0, 17.1, 17.2,
];

const DRAWDOWN_DEFAULT = [
  0.0, 0.0, -0.35, -0.15,
  -0.82, -0.82, -1.48,
  -0.55, -0.65, 0.0,
  -0.25, 0.0, -0.52,
  0.0, -0.60, -0.12,
  -1.15, -1.96, -1.32,
  -0.18, 0.0, -0.18,
  0.0, 0.0, 0.0,
  0.0, 0.0, 0.0,
];

const SHARPE_DEFAULT = [
  1.18, 1.22, 1.26, 1.31,
  1.35, 1.33, 1.24,
  1.18, 1.25, 1.42,
  1.58, 1.66, 1.62,
  1.48, 1.36, 1.39,
  1.52, 1.69, 1.84,
  1.88, 1.82, 1.74,
  1.68, 1.71, 1.76,
  1.80, 1.82, 1.84,
];

export const DEFAULT_MONTHLY_PNL: PeriodPnlPoint[] = [
  { period: 'Mar', pnl: 21400 },
  { period: 'Apr', pnl: 28200 },
  { period: 'May', pnl: 34100 },
  { period: 'Jun', pnl: 41800 },
  { period: 'Jul', pnl: 17500 },
  { period: 'Aug', pnl: -12400 },
  { period: 'Sep', pnl: 31200 },
  { period: 'Oct', pnl: 44800 },
  { period: 'Nov', pnl: 42000 },
];

export function buildBacktestResult(config: BacktestConfig, id = 'run-current', name = 'Current Run'): BacktestResult {
  const strategyFactorMap: Record<string, { retMult: number; sharpeOffset: number; winRate: number; ddMult: number }> = {
    'Breakout v2': { retMult: 1.0, sharpeOffset: 0, winRate: 58.5, ddMult: 1.0 },
    Momentum: { retMult: 0.91, sharpeOffset: -0.18, winRate: 54.2, ddMult: 1.25 },
    'Mean reversion': { retMult: 0.78, sharpeOffset: -0.24, winRate: 62.1, ddMult: 0.88 },
    'EMA Crossover': { retMult: 0.84, sharpeOffset: -0.31, winRate: 51.6, ddMult: 1.38 },
  };

  const instrumentMultMap: Record<string, number> = {
    BANKNIFTY: 1.0,
    'NIFTY 50': 0.88,
    SENSEX: 0.92,
    'NIFTY IT': 0.76,
    RELIANCE: 1.06,
    HDFCBANK: 0.94,
    ICICIBANK: 0.98,
    INFY: 0.72,
    TCS: 0.81,
  };

  const sf = strategyFactorMap[config.strategy] ?? strategyFactorMap['Breakout v2'];
  const instMult = instrumentMultMap[config.instrumentSymbol] ?? 0.95;
  const slippagePenalty = Math.max(0.85, 1 - (config.slippagePct - 0.05) * 1.1);
  const netMult = sf.retMult * instMult * slippagePenalty;

  const series: BacktestSeriesPoint[] = BASE_DATES.map((date, idx) => {
    const stratRet = Number((STRATEGY_RETURNS_DEFAULT[idx] * netMult).toFixed(2));
    const buyHold = Number((BUYHOLD_RETURNS_DEFAULT[idx] * instMult).toFixed(2));
    const equity = Math.round(config.initialCapital * (1 + stratRet / 100));
    const drawdownPct = Number((DRAWDOWN_DEFAULT[idx] * sf.ddMult).toFixed(2));
    const rollingSharpe = Number(Math.max(0.2, SHARPE_DEFAULT[idx] + sf.sharpeOffset).toFixed(2));
    return {
      date,
      equity,
      strategyReturnPct: stratRet,
      buyHoldReturnPct: buyHold,
      rollingSharpe,
      drawdownPct,
    };
  });

  const lastPoint = series[series.length - 1];
  const netReturnPct = lastPoint.strategyReturnPct;
  const netProfit = Math.round((config.initialCapital * netReturnPct) / 100);
  const maxDrawdownPct = Number(Math.min(...series.map((s) => s.drawdownPct)).toFixed(2));
  const sharpeRatio = Number((1.84 + sf.sharpeOffset * 0.8).toFixed(2));
  const totalTrades = 248;
  const wins = Math.round((totalTrades * sf.winRate) / 100);

  const scaleFactor = netProfit / 248600;
  const monthlyPnl: PeriodPnlPoint[] = DEFAULT_MONTHLY_PNL.map((m) => ({
    period: m.period,
    pnl: Math.round(m.pnl * scaleFactor),
  }));

  return {
    id,
    name,
    config,
    netReturnPct,
    netProfit,
    maxDrawdownPct,
    sharpeRatio,
    winRatePct: sf.winRate,
    wins,
    totalTrades,
    series,
    monthlyPnl,
    createdAt: '30 Nov 2026 · 10:45 IST',
  };
}

export const INITIAL_BACKTEST_CONFIG: BacktestConfig = {
  strategy: 'Breakout v2',
  instrumentSymbol: 'BANKNIFTY',
  initialCapital: 1000000,
  dateRange: '01 Mar – 30 Nov 2026',
  timeframe: '15m candles',
  lookbackPeriod: 20,
  stopLossPct: 0.75,
  targetPct: 1.5,
  slippagePct: 0.05,
};

export const SAVED_BACKTEST_RUNS: BacktestResult[] = [
  buildBacktestResult(INITIAL_BACKTEST_CONFIG, 'run-1', 'Breakout v2 · BANKNIFTY (Baseline)'),
  buildBacktestResult(
    {
      ...INITIAL_BACKTEST_CONFIG,
      strategy: 'Momentum',
      instrumentSymbol: 'RELIANCE',
      timeframe: '1H candles',
    },
    'run-2',
    'Momentum · RELIANCE 1H'
  ),
  buildBacktestResult(
    {
      ...INITIAL_BACKTEST_CONFIG,
      strategy: 'Mean reversion',
      instrumentSymbol: 'NIFTY 50',
      timeframe: '5m candles',
    },
    'run-3',
    'Mean Reversion · NIFTY 50 5m'
  ),
];
