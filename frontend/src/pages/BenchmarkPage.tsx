import { useState } from 'react';
import { PageHeader } from '@/components/layout/PageHeader';
import { PageFooter } from '@/components/layout/PageFooter';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { KpiCard } from '@/components/shared/KpiCard';
import { ChartToggleTabs } from '@/components/shared/ChartToggleTabs';
import { QueryStateWrapper } from '@/components/shared/QueryStateWrapper';
import { LineChartCard } from '@/components/charts/LineChartCard';
import { AreaDrawdownChart } from '@/components/charts/AreaDrawdownChart';
import { PnlBarChart } from '@/components/charts/PnlBarChart';
import { INITIAL_BENCHMARK_CONFIG } from '@/data/mockBenchmark';
import { cn, formatCompactLakhs, formatINR } from '@/lib/utils';
import { useAppStore } from '@/store/useAppStore';
import { useBenchmarkQuery } from '@/hooks/useTradingQueries';
import type { BacktestChartTab, BenchmarkConfig } from '@/types/trading';

const STRATEGIES = ['Breakout v2', 'Momentum', 'Mean reversion', 'EMA Crossover'];
const BENCHMARKS = ['NIFTY 50', 'BANKNIFTY', 'SENSEX', 'NIFTY IT', 'NIFTY 500'];
const DATE_RANGES = ['01 Mar – 30 Nov 2026', 'Last 6 Months', 'Last 1 Year', 'YTD 2026'];
const CAPITALS = [500000, 1000000, 2500000, 5000000];
const TIMEFRAMES = ['5 minutes', '15 minutes', '1 hour', '1 day'];

export function BenchmarkPage() {
  const pushToast = useAppStore((s) => s.pushToast);
  const numberFormat = useAppStore((s) => s.settings.regional.numberFormat);

  const [draftConfig, setDraftConfig] = useState<BenchmarkConfig>(INITIAL_BENCHMARK_CONFIG);
  const [appliedConfig, setAppliedConfig] = useState<BenchmarkConfig>(INITIAL_BENCHMARK_CONFIG);
  const [activeChartTab, setActiveChartTab] = useState<BacktestChartTab>('comparison');

  const { data: result, isLoading, isError, refetch } = useBenchmarkQuery(appliedConfig);

  const handleRunBenchmark = () => {
    setAppliedConfig({ ...draftConfig });
    pushToast({
      title: `Benchmark updated: ${draftConfig.strategy} vs ${draftConfig.benchmark}`,
      description: `${draftConfig.dateRange} · ${draftConfig.timeframe}`,
      variant: 'success',
    });
  };

  return (
    <div className="space-y-4">
      <PageHeader
        title="Benchmark"
        subtitle="Measure your strategies against the market."
        actions={
          <Badge variant="slate" className="h-8 px-3.5 text-[11px] font-semibold tracking-wider">
            SIMULATED RESULTS
          </Badge>
        }
      />

      {/* Benchmark Configuration Card */}
      <Card className="p-5">
        <div className="mb-4">
          <h2 className="text-[15px] font-semibold text-[#18181B]">Run a Benchmark</h2>
          <p className="mt-0.5 text-[11.5px] text-[#71717A]">
            Compare the same period, capital and execution assumptions.
          </p>
        </div>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-6">
          <div className="space-y-1.5">
            <Label>Strategy</Label>
            <Select
              value={draftConfig.strategy}
              onValueChange={(v) => setDraftConfig((p) => ({ ...p, strategy: v }))}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {STRATEGIES.map((s) => (
                  <SelectItem key={s} value={s}>
                    {s}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-1.5">
            <Label>Benchmark</Label>
            <Select
              value={draftConfig.benchmark}
              onValueChange={(v) => setDraftConfig((p) => ({ ...p, benchmark: v }))}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {BENCHMARKS.map((b) => (
                  <SelectItem key={b} value={b}>
                    {b}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-1.5">
            <Label>Date range</Label>
            <Select
              value={draftConfig.dateRange}
              onValueChange={(v) => setDraftConfig((p) => ({ ...p, dateRange: v }))}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {DATE_RANGES.map((d) => (
                  <SelectItem key={d} value={d}>
                    {d}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-1.5">
            <Label>Initial capital</Label>
            <Select
              value={String(draftConfig.initialCapital)}
              onValueChange={(v) =>
                setDraftConfig((p) => ({ ...p, initialCapital: Number(v) }))
              }
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {CAPITALS.map((c) => (
                  <SelectItem key={c} value={String(c)}>
                    {formatINR(c, { numberFormat })}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-1.5">
            <Label>Timeframe</Label>
            <Select
              value={draftConfig.timeframe}
              onValueChange={(v) => setDraftConfig((p) => ({ ...p, timeframe: v }))}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {TIMEFRAMES.map((tf) => (
                  <SelectItem key={tf} value={tf}>
                    {tf}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="flex items-end">
            <Button onClick={handleRunBenchmark} className="w-full">
              Run benchmark
            </Button>
          </div>
        </div>
      </Card>

      <QueryStateWrapper isLoading={isLoading} isError={isError} onRetry={() => refetch()}>
        {result && (
          <>
            {/* Strategy Comparison KPI Cards */}
            <div className="grid grid-cols-1 gap-3.5 sm:grid-cols-2 xl:grid-cols-4">
              <KpiCard
                label="Strategy return"
                value={`${result.strategyReturnPct >= 0 ? '+' : '−'}${Math.abs(result.strategyReturnPct).toFixed(2)}%`}
                subtext={`${appliedConfig.strategy} · ${formatINR(result.strategyProfit, { showSign: true, numberFormat })}`}
                tone={result.strategyReturnPct >= 0 ? 'positive' : 'negative'}
              />
              <KpiCard
                label="Benchmark return"
                value={`${result.benchmarkReturnPct >= 0 ? '+' : '−'}${Math.abs(result.benchmarkReturnPct).toFixed(2)}%`}
                subtext={`${appliedConfig.benchmark} · ${formatINR(result.benchmarkProfit, { showSign: true, numberFormat })}`}
                tone={result.benchmarkReturnPct >= 0 ? 'positive' : 'negative'}
              />
              <KpiCard
                label="Excess return"
                value={`${result.excessReturnPp >= 0 ? '+' : '−'}${Math.abs(result.excessReturnPp).toFixed(2)} pp`}
                subtext={
                  result.excessReturnPp >= 0
                    ? 'Outperformed the benchmark'
                    : 'Underperformed the benchmark'
                }
                tone={result.excessReturnPp >= 0 ? 'positive' : 'negative'}
              />
              <KpiCard
                label="Sharpe ratio"
                value={result.strategySharpe.toFixed(2)}
                subtext={`Strategy · Benchmark ${result.benchmarkSharpe.toFixed(2)}`}
              />
            </div>

            {/* Chart Selection Controls Row */}
            <div className="flex flex-wrap items-center justify-between gap-3 pt-1">
              <ChartToggleTabs value={activeChartTab} onChange={setActiveChartTab} />
              <span className="hidden text-[11.5px] text-[#71717A] sm:inline">
                {appliedConfig.strategy} vs {appliedConfig.benchmark}
              </span>
            </div>

            {/* Graph 1: Switchable Line Chart (Equity Curve / Strategy vs Buy-and-Hold / Rolling Sharpe Ratio) */}
            {activeChartTab === 'equity' && (
              <LineChartCard
                title="Equity Curve"
                subtitle="Portfolio equity normalised to starting capital after fees and slippage · INR"
                data={result.series}
                xKey="date"
                series={[
                  {
                    dataKey: 'strategyEquity',
                    name: appliedConfig.strategy,
                    color: '#52647A',
                    strokeWidth: 1.9,
                  },
                  {
                    dataKey: 'benchmarkEquity',
                    name: appliedConfig.benchmark,
                    color: '#9CA3AF',
                    dashed: true,
                    strokeWidth: 1.5,
                  },
                ]}
                yFormatter={formatCompactLakhs}
                tooltipFormatter={(v) => formatINR(v, { numberFormat })}
                height={235}
              />
            )}

            {activeChartTab === 'comparison' && (
              <LineChartCard
                title="Strategy vs Buy-and-Hold"
                subtitle="Cumulative returns · Normalised to the same starting capital · After fees and slippage"
                data={result.series}
                xKey="date"
                series={[
                  {
                    dataKey: 'strategyReturnPct',
                    name: appliedConfig.strategy,
                    color: '#52647A',
                    strokeWidth: 1.9,
                  },
                  {
                    dataKey: 'benchmarkReturnPct',
                    name: appliedConfig.benchmark,
                    color: '#9CA3AF',
                    dashed: true,
                    strokeWidth: 1.5,
                  },
                ]}
                yFormatter={(v) => `${v}%`}
                tooltipFormatter={(v) => `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`}
                height={235}
              />
            )}

            {activeChartTab === 'sharpe' && (
              <LineChartCard
                title="Rolling Sharpe Ratio"
                subtitle="60-day rolling window · Annualised"
                data={result.series}
                xKey="date"
                series={[
                  {
                    dataKey: 'strategyRollingSharpe',
                    name: appliedConfig.strategy,
                    color: '#52647A',
                    strokeWidth: 1.9,
                  },
                  {
                    dataKey: 'benchmarkRollingSharpe',
                    name: appliedConfig.benchmark,
                    color: '#9CA3AF',
                    dashed: true,
                    strokeWidth: 1.5,
                  },
                ]}
                yDomain={[0, 3]}
                yFormatter={(v) => v.toFixed(2)}
                tooltipFormatter={(v) => v.toFixed(2)}
                height={235}
              />
            )}

            {/* Graphs 2 & 3: Drawdown Comparison + Trading P&L */}
            <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
              <AreaDrawdownChart
                title="Drawdown"
                subtitle="Peak-to-trough decline · %"
                headerRight={
                  <div className="flex items-center gap-4 text-[11.5px]">
                    <span className="font-medium text-[#DC2626]">
                      {appliedConfig.strategy}
                    </span>
                    <span className="text-[#71717A]">{appliedConfig.benchmark}</span>
                  </div>
                }
                data={result.series}
                xKey="date"
                series={[
                  {
                    dataKey: 'strategyDrawdownPct',
                    name: appliedConfig.strategy,
                    color: '#DC2626',
                    fillOpacity: 0.08,
                  },
                  {
                    dataKey: 'benchmarkDrawdownPct',
                    name: appliedConfig.benchmark,
                    color: '#9CA3AF',
                    dashed: true,
                    fillOpacity: 0,
                  },
                ]}
                height={200}
              />

              <PnlBarChart
                title="Trading P&L"
                subtitle="Monthly realised result · INR"
                headerRight={
                  <span
                    className={cn(
                      'text-[12px] font-semibold tabular-nums',
                      result.netMonthlyPnl >= 0 ? 'text-[#16A34A]' : 'text-[#DC2626]'
                    )}
                  >
                    Net {formatINR(result.netMonthlyPnl, { showSign: true, numberFormat })}
                  </span>
                }
                data={result.monthlyPnl}
                xKey="period"
                valueKey="pnl"
                height={200}
              />
            </div>

            {/* Additional Sections: Quantitative Performance Metrics + All Strategies Benchmark Comparison */}
            <div className="grid grid-cols-1 gap-4 xl:grid-cols-12">
              {/* Quantitative Performance Metrics Panel */}
              <Card className="p-5 xl:col-span-5">
                <div className="mb-3">
                  <h3 className="text-[15px] font-semibold text-[#18181B]">
                    Quantitative Performance Metrics
                  </h3>
                  <p className="mt-0.5 text-[11.5px] text-[#71717A]">
                    {appliedConfig.strategy} vs {appliedConfig.benchmark}
                  </p>
                </div>

                <div className="overflow-hidden rounded-lg border border-[#E4E4E7]">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Metric</TableHead>
                        <TableHead className="text-right">Strategy</TableHead>
                        <TableHead className="text-right">Benchmark</TableHead>
                        <TableHead className="text-right">Delta</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {result.quantitativeMetrics.map((m) => (
                        <TableRow key={m.label}>
                          <TableCell className="font-medium text-[#18181B]">
                            {m.label}
                          </TableCell>
                          <TableCell className="text-right font-semibold tabular-nums">
                            {m.strategyValue}
                          </TableCell>
                          <TableCell className="text-right text-[#71717A] tabular-nums">
                            {m.benchmarkValue}
                          </TableCell>
                          <TableCell
                            className={cn(
                              'text-right text-[12px] font-medium tabular-nums',
                              m.isPositiveDelta ? 'text-[#16A34A]' : 'text-[#71717A]'
                            )}
                          >
                            {m.delta}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              </Card>

              {/* All Strategies & Strategy-wise Benchmark Comparison */}
              <Card className="p-5 xl:col-span-7">
                <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <h3 className="text-[15px] font-semibold text-[#18181B]">
                      All Strategies Benchmark Comparison
                    </h3>
                    <p className="mt-0.5 text-[11.5px] text-[#71717A]">
                      Click any strategy row to load it against {appliedConfig.benchmark}
                    </p>
                  </div>
                  <Badge variant="outline">{appliedConfig.benchmark}</Badge>
                </div>

                <div className="overflow-hidden rounded-lg border border-[#E4E4E7]">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Strategy</TableHead>
                        <TableHead className="text-right">Return</TableHead>
                        <TableHead className="text-right">Excess</TableHead>
                        <TableHead className="text-right">Sharpe</TableHead>
                        <TableHead className="text-right">Sortino</TableHead>
                        <TableHead className="text-right">Alpha</TableHead>
                        <TableHead className="text-right">Beta</TableHead>
                        <TableHead className="text-right">Max DD</TableHead>
                        <TableHead className="text-right">Win Rate</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {result.allStrategies.map((row) => {
                        const isSelected = row.strategy === appliedConfig.strategy;
                        return (
                          <TableRow
                            key={row.strategy}
                            onClick={() => {
                              const next = { ...draftConfig, strategy: row.strategy };
                              setDraftConfig(next);
                              setAppliedConfig(next);
                            }}
                            className={cn(
                              'cursor-pointer',
                              isSelected && 'bg-[#F1F5F9]/80'
                            )}
                          >
                            <TableCell className="font-semibold text-[#18181B]">
                              {row.strategy}
                            </TableCell>
                            <TableCell className="text-right font-medium text-[#16A34A] tabular-nums">
                              +{row.totalReturnPct.toFixed(2)}%
                            </TableCell>
                            <TableCell
                              className={cn(
                                'text-right font-medium tabular-nums',
                                row.excessReturnPp >= 0 ? 'text-[#16A34A]' : 'text-[#DC2626]'
                              )}
                            >
                              {row.excessReturnPp >= 0 ? '+' : '−'}
                              {Math.abs(row.excessReturnPp).toFixed(2)} pp
                            </TableCell>
                            <TableCell className="text-right tabular-nums">
                              {row.sharpeRatio.toFixed(2)}
                            </TableCell>
                            <TableCell className="text-right tabular-nums">
                              {row.sortinoRatio.toFixed(2)}
                            </TableCell>
                            <TableCell className="text-right text-[#16A34A] tabular-nums">
                              +{row.alpha.toFixed(2)}%
                            </TableCell>
                            <TableCell className="text-right tabular-nums">
                              {row.beta.toFixed(2)}
                            </TableCell>
                            <TableCell className="text-right text-[#DC2626] tabular-nums">
                              {row.maxDrawdownPct.toFixed(2)}%
                            </TableCell>
                            <TableCell className="text-right tabular-nums">
                              {row.winRatePct.toFixed(1)}%
                            </TableCell>
                          </TableRow>
                        );
                      })}
                    </TableBody>
                  </Table>
                </div>
              </Card>
            </div>
          </>
        )}
      </QueryStateWrapper>

      <PageFooter leftText="Sample data · Virtual capital only · All values in INR" />
    </div>
  );
}
