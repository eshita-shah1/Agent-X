import { useQuery, useMutation } from '@tanstack/react-query';
import { MOCK_INSTRUMENTS } from '@/data/mockInstruments';
import { MOCK_HOLDINGS, PORTFOLIO_SERIES_BY_TIMEFRAME } from '@/data/mockPortfolio';
import { buildBacktestResult } from '@/data/mockBacktest';
import { buildBenchmarkResult } from '@/data/mockBenchmark';
import { generateCandleSeries } from '@/lib/marketData';
import type {
  BacktestConfig,
  BenchmarkConfig,
  ChartTimeframe,
  PortfolioTimeframe,
} from '@/types/trading';

export function useInstrumentsQuery() {
  return useQuery({
    queryKey: ['instruments'],
    queryFn: async () => MOCK_INSTRUMENTS,
    staleTime: 60_000,
  });
}

export function useCandleSeriesQuery(symbol: string, timeframe: ChartTimeframe) {
  return useQuery({
    queryKey: ['candles', symbol, timeframe],
    queryFn: async () => {
      const instrument =
        MOCK_INSTRUMENTS.find((i) => i.symbol === symbol) ?? MOCK_INSTRUMENTS[1];
      return {
        instrument,
        bars: generateCandleSeries(instrument, timeframe),
      };
    },
    staleTime: 30_000,
  });
}

export function usePortfolioQuery(timeframe: PortfolioTimeframe) {
  return useQuery({
    queryKey: ['portfolio', timeframe],
    queryFn: async () => ({
      holdings: MOCK_HOLDINGS,
      series: PORTFOLIO_SERIES_BY_TIMEFRAME[timeframe],
    }),
    staleTime: 30_000,
  });
}

export function useBacktestQuery(config: BacktestConfig) {
  return useQuery({
    queryKey: ['backtest', config],
    queryFn: async () => {
      await new Promise((resolve) => setTimeout(resolve, 180));
      return buildBacktestResult(config);
    },
    staleTime: 60_000,
  });
}

export function useRunBacktestMutation() {
  return useMutation({
    mutationFn: async (config: BacktestConfig) => {
      await new Promise((resolve) => setTimeout(resolve, 420));
      return buildBacktestResult(config, `run-${Date.now()}`, `${config.strategy} · ${config.instrumentSymbol}`);
    },
  });
}

export function useBenchmarkQuery(config: BenchmarkConfig) {
  return useQuery({
    queryKey: ['benchmark', config],
    queryFn: async () => {
      await new Promise((resolve) => setTimeout(resolve, 180));
      return buildBenchmarkResult(config);
    },
    staleTime: 60_000,
  });
}
