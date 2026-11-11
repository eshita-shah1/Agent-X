import type {
  BenchmarkConfig,
  BenchmarkResult,
  BenchmarkSeriesPoint,
  PeriodPnlPoint,
  QuantitativeMetric,
  StrategyComparisonRow,
} from '@/types/trading';

const DATES = [
  'Mar 01', 'Mar 12', 'Mar 24',
  'Apr 05', 'Apr 17', 'Apr 29',
  'May 11', 'May 23',
  'Jun 04', 'Jun 16', 'Jun 28',
  'Jul 10', 'Jul 22',
  'Aug 03', 'Aug 15', 'Aug 27',
  'Sep 08', 'Sep 20',
  'Oct 02', 'Oct 14', 'Oct 26',
  'Nov 07', 'Nov 19', 'Nov 30',
];

const STRAT_CUM_BASE = [
  0.0, 0.8, 1.4,
  1.1, 1.8, 1.4,
  1.9, 3.8,
  5.7, 5.4, 7.6,
  10.8, 10.2,
  11.4, 9.7, 11.4,
  10.1, 12.6,
  14.2, 16.5, 18.2,
  20.1, 22.8, 24.86,
];

const BENCH_CUM_BASE = [
  0.0, 1.1, 2.0,
  2.6, 2.4, 2.8,
  3.4, 4.9,
  6.2, 6.9, 7.7,
  8.4, 8.8,
  9.2, 9.8, 10.7,
  12.1, 13.4,
  14.1, 14.5, 14.9,
  15.3, 16.2, 17.2,
];

const STRAT_DD_BASE = [
  0.0, -0.2, -0.6,
  -1.4, -0.8, -0.3,
  -0.9, -0.1,
  0.0, -0.3, -0.6,
  -1.2, -1.96,
  -1.1, -0.4, 0.0,
  -0.4, 0.0,
  0.0, 0.0, 0.0,
  0.0, 0.0, 0.0,
];

const BENCH_DD_BASE = [
  0.0, -0.9, -2.5,
  -1.7, -4.1, -1.6,
  -2.2, -3.1,
  -1.5, -3.4, -5.2,
  -6.4, -8.1,
  -5.4, -3.8, -2.1,
  -1.2, -0.5,
  -0.2, -0.6, -0.3,
  -0.1, 0.0, 0.0,
];

const STRAT_SHARPE_BASE = [
  1.18, 1.24, 1.30,
  1.28, 1.32, 1.25,
  1.20, 1.34,
  1.52, 1.60, 1.66,
  1.58, 1.44,
  1.48, 1.56, 1.72,
  1.84, 1.88,
  1.80, 1.74, 1.76,
  1.81, 1.83, 1.84,
];

const BENCH_SHARPE_BASE = [
  0.88, 0.92, 0.96,
  0.94, 0.91, 0.95,
  0.98, 1.04,
  1.10, 1.14, 1.12,
  1.06, 0.99,
  1.02, 1.07, 1.11,
  1.15, 1.16,
  1.13, 1.10, 1.09,
  1.11, 1.12, 1.12,
];

const MONTHLY_PNL_BASE: PeriodPnlPoint[] = [
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

export const INITIAL_BENCHMARK_CONFIG: BenchmarkConfig = {
  strategy: 'Breakout v2',
  benchmark: 'NIFTY 50',
  dateRange: '01 Mar – 30 Nov 2026',
  initialCapital: 1000000,
  timeframe: '15 minutes',
};

export function buildBenchmarkResult(config: BenchmarkConfig): BenchmarkResult {
  const stratProfile: Record<string, { mult: number; sharpe: number; sortino: number; winRate: number; beta: number; ddMult: number }> = {
    'Breakout v2': { mult: 1.0, sharpe: 1.84, sortino: 2.41, winRate: 58.5, beta: 0.74, ddMult: 1.0 },
    Momentum: { mult: 0.91, sharpe: 1.66, sortino: 2.15, winRate: 54.2, beta: 0.86, ddMult: 1.32 },
    'Mean reversion': { mult: 0.78, sharpe: 1.54, sortino: 1.98, winRate: 62.1, beta: 0.52, ddMult: 0.92 },
    'EMA Crossover': { mult: 0.83, sharpe: 1.41, sortino: 1.82, winRate: 51.6, beta: 0.79, ddMult: 1.44 },
  };

  const benchProfile: Record<string, { mult: number; sharpe: number }> = {
    'NIFTY 50': { mult: 1.0, sharpe: 1.12 },
    BANKNIFTY: { mult: 1.16, sharpe: 1.19 },
    SENSEX: { mult: 0.96, sharpe: 1.08 },
    'NIFTY IT': { mult: 0.62, sharpe: 0.74 },
    'NIFTY 500': { mult: 1.05, sharpe: 1.14 },
  };

  const dateMult: Record<string, number> = {
    '01 Mar – 30 Nov 2026': 1.0,
    'Last 6 Months': 0.76,
    'Last 1 Year': 1.24,
    'YTD 2026': 1.11,
  };

  const sp = stratProfile[config.strategy] ?? stratProfile['Breakout v2'];
  const bp = benchProfile[config.benchmark] ?? benchProfile['NIFTY 50'];
  const dm = dateMult[config.dateRange] ?? 1.0;

  const series: BenchmarkSeriesPoint[] = DATES.map((date, idx) => {
    const strategyReturnPct = Number((STRAT_CUM_BASE[idx] * sp.mult * dm).toFixed(2));
    const benchmarkReturnPct = Number((BENCH_CUM_BASE[idx] * bp.mult * dm).toFixed(2));
    return {
      date,
      strategyEquity: Math.round(config.initialCapital * (1 + strategyReturnPct / 100)),
      benchmarkEquity: Math.round(config.initialCapital * (1 + benchmarkReturnPct / 100)),
      strategyReturnPct,
      benchmarkReturnPct,
      strategyRollingSharpe: Number(
        Math.max(0.25, STRAT_SHARPE_BASE[idx] + (sp.sharpe - 1.84)).toFixed(2)
      ),
      benchmarkRollingSharpe: Number(
        Math.max(0.2, BENCH_SHARPE_BASE[idx] + (bp.sharpe - 1.12)).toFixed(2)
      ),
      strategyDrawdownPct: Number((STRAT_DD_BASE[idx] * sp.ddMult).toFixed(2)),
      benchmarkDrawdownPct: Number((BENCH_DD_BASE[idx] * (0.9 + bp.mult * 0.1)).toFixed(2)),
    };
  });

  const last = series[series.length - 1];
  const strategyReturnPct = last.strategyReturnPct;
  const benchmarkReturnPct = last.benchmarkReturnPct;
  const strategyProfit = Math.round((config.initialCapital * strategyReturnPct) / 100);
  const benchmarkProfit = Math.round((config.initialCapital * benchmarkReturnPct) / 100);
  const excessReturnPp = Number((strategyReturnPct - benchmarkReturnPct).toFixed(2));
  const maxStratDd = Number(Math.min(...series.map((p) => p.strategyDrawdownPct)).toFixed(2));
  const maxBenchDd = Number(Math.min(...series.map((p) => p.benchmarkDrawdownPct)).toFixed(2));

  const scaleFactor = strategyProfit / 248600;
  const monthlyPnl = MONTHLY_PNL_BASE.map((m) => ({
    period: m.period,
    pnl: Math.round(m.pnl * scaleFactor),
  }));

  const alpha = Number((strategyReturnPct - (6 + sp.beta * (benchmarkReturnPct - 6))).toFixed(2));

  const quantitativeMetrics: QuantitativeMetric[] = [
    {
      label: 'Annualised Alpha',
      strategyValue: `${alpha >= 0 ? '+' : ''}${alpha.toFixed(2)}%`,
      benchmarkValue: '0.00%',
      delta: `${alpha >= 0 ? '+' : ''}${alpha.toFixed(2)} pp`,
      positiveBetter: true,
      isPositiveDelta: alpha >= 0,
    },
    {
      label: 'Market Beta',
      strategyValue: sp.beta.toFixed(2),
      benchmarkValue: '1.00',
      delta: `${(sp.beta - 1).toFixed(2)} vs index`,
      positiveBetter: false,
      isPositiveDelta: sp.beta < 1,
    },
    {
      label: 'Sortino Ratio',
      strategyValue: sp.sortino.toFixed(2),
      benchmarkValue: '1.46',
      delta: `+${(sp.sortino - 1.46).toFixed(2)}`,
      positiveBetter: true,
      isPositiveDelta: sp.sortino > 1.46,
    },
    {
      label: 'Information Ratio',
      strategyValue: '1.38',
      benchmarkValue: '—',
      delta: 'Top quartile',
      positiveBetter: true,
      isPositiveDelta: true,
    },
    {
      label: 'Calmar Ratio',
      strategyValue: (strategyReturnPct / Math.abs(maxStratDd)).toFixed(2),
      benchmarkValue: (benchmarkReturnPct / Math.abs(maxBenchDd)).toFixed(2),
      delta: 'Superior recovery',
      positiveBetter: true,
      isPositiveDelta: true,
    },
    {
      label: 'Annualised Volatility',
      strategyValue: '9.84%',
      benchmarkValue: '14.20%',
      delta: '-4.36% lower',
      positiveBetter: false,
      isPositiveDelta: true,
    },
    {
      label: 'Maximum Drawdown',
      strategyValue: `${maxStratDd.toFixed(2)}%`,
      benchmarkValue: `${maxBenchDd.toFixed(2)}%`,
      delta: `+${(Math.abs(maxBenchDd) - Math.abs(maxStratDd)).toFixed(2)} pp protected`,
      positiveBetter: true,
      isPositiveDelta: Math.abs(maxStratDd) < Math.abs(maxBenchDd),
    },
    {
      label: 'Profit Factor',
      strategyValue: '2.14',
      benchmarkValue: '1.42',
      delta: '+0.72',
      positiveBetter: true,
      isPositiveDelta: true,
    },
  ];

  const allStrategies: StrategyComparisonRow[] = Object.entries(stratProfile).map(([name, prof]) => {
    const ret = Number((24.86 * prof.mult * dm).toFixed(2));
    const excess = Number((ret - benchmarkReturnPct).toFixed(2));
    const dd = Number((-1.96 * prof.ddMult).toFixed(2));
    const a = Number((ret - (6 + prof.beta * (benchmarkReturnPct - 6))).toFixed(2));
    return {
      strategy: name,
      benchmark: config.benchmark,
      totalReturnPct: ret,
      benchmarkReturnPct,
      excessReturnPp: excess,
      sharpeRatio: prof.sharpe,
      sortinoRatio: prof.sortino,
      maxDrawdownPct: dd,
      alpha: a,
      beta: prof.beta,
      winRatePct: prof.winRate,
    };
  });

  return {
    config,
    strategyReturnPct,
    strategyProfit,
    benchmarkReturnPct,
    benchmarkProfit,
    excessReturnPp,
    strategySharpe: sp.sharpe,
    benchmarkSharpe: bp.sharpe,
    netMonthlyPnl: strategyProfit,
    series,
    monthlyPnl,
    quantitativeMetrics,
    allStrategies,
  };
}
