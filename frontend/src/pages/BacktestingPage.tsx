import { useEffect, useRef, useState } from 'react';
import { ArrowDown, Calendar, ChevronDown, FolderClock, Play, SlidersHorizontal } from 'lucide-react';
import { PageHeader } from '@/components/layout/PageHeader';
import { PageFooter } from '@/components/layout/PageFooter';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { KpiCard } from '@/components/shared/KpiCard';
import { ChartToggleTabs } from '@/components/shared/ChartToggleTabs';
import { QueryStateWrapper } from '@/components/shared/QueryStateWrapper';
import { LineChartCard } from '@/components/charts/LineChartCard';
import { AreaDrawdownChart } from '@/components/charts/AreaDrawdownChart';
import { PnlBarChart } from '@/components/charts/PnlBarChart';
import { INITIAL_BACKTEST_CONFIG, SAVED_BACKTEST_RUNS } from '@/data/mockBacktest';
import { cn, formatCompactLakhs, formatINR } from '@/lib/utils';
import { useAppStore } from '@/store/useAppStore';
import { useBacktestQuery, useRunBacktestMutation } from '@/hooks/useTradingQueries';
import type { BacktestChartTab, BacktestConfig, BacktestResult } from '@/types/trading';

const STRATEGIES = ['Breakout v2', 'Momentum', 'Mean reversion', 'EMA Crossover'];
const DATE_RANGES = ['01 Mar – 30 Nov 2026', 'Last 6 Months', 'Last 1 Year', 'YTD 2026'];
const TIMEFRAME_OPTIONS = ['5m candles', '15m candles', '1H candles', '1D candles'];

export function BacktestingPage() {
  const selectedSymbol = useAppStore((s) => s.selectedInstrumentSymbol);
  const setSelectedInstrument = useAppStore((s) => s.setSelectedInstrument);
  const pushToast = useAppStore((s) => s.pushToast);
  const numberFormat = useAppStore((s) => s.settings.regional.numberFormat);

  const [config, setConfig] = useState<BacktestConfig>({
    ...INITIAL_BACKTEST_CONFIG,
    instrumentSymbol: selectedSymbol,
  });
  const [activeTab, setActiveTab] = useState<BacktestChartTab>('equity');
  const [savedRuns, setSavedRuns] = useState<BacktestResult[]>(SAVED_BACKTEST_RUNS);
  const [savedRunsOpen, setSavedRunsOpen] = useState(false);
  const [paramsOpen, setParamsOpen] = useState(false);

  const tradingPnlRef = useRef<HTMLDivElement | null>(null);

  // Sync instrument changes from Right Instruments Panel
  useEffect(() => {
    setConfig((prev) =>
      prev.instrumentSymbol === selectedSymbol
        ? prev
        : { ...prev, instrumentSymbol: selectedSymbol }
    );
  }, [selectedSymbol]);

  const { data: queryResult, isLoading, isError, refetch } = useBacktestQuery(config);
  const runMutation = useRunBacktestMutation();
  const [overrideResult, setOverrideResult] = useState<BacktestResult | null>(null);

  // Clear mutation override whenever config changes
  useEffect(() => {
    setOverrideResult(null);
  }, [config]);

  const result = overrideResult ?? queryResult;

  const handleRunBacktest = async () => {
    const newRun = await runMutation.mutateAsync(config);
    setOverrideResult(newRun);
    setSavedRuns((prev) => [newRun, ...prev.slice(0, 7)]);
    pushToast({
      title: `Backtest complete: ${config.strategy} on ${config.instrumentSymbol}`,
      description: `Net return ${newRun.netReturnPct >= 0 ? '+' : ''}${newRun.netReturnPct.toFixed(2)}% · Sharpe ${newRun.sharpeRatio.toFixed(2)}`,
      variant: 'success',
    });
  };

  return (
    <div className="space-y-4">
      <PageHeader
        title="Backtesting Lab"
        subtitle="Validate your edge before putting it to work."
        actions={
          <>
            <Button variant="outline" onClick={() => setSavedRunsOpen(true)}>
              <FolderClock className="h-3.5 w-3.5 text-[#71717A]" />
              Saved runs
            </Button>
            <Button
              variant="default"
              disabled={runMutation.isPending}
              onClick={handleRunBacktest}
            >
              <Play className="h-3.5 w-3.5" />
              {runMutation.isPending ? 'Running...' : 'Run backtest'}
            </Button>
          </>
        }
      />

      {/* Strategy Configuration Bar */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button
                type="button"
                className="inline-flex items-center gap-1.5 text-[15px] font-semibold text-[#18181B] hover:text-[#52647A]"
              >
                {config.strategy}
                <ChevronDown className="h-3.5 w-3.5 text-[#71717A]" />
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start">
              {STRATEGIES.map((s) => (
                <DropdownMenuItem
                  key={s}
                  onClick={() => setConfig((prev) => ({ ...prev, strategy: s }))}
                >
                  {s}
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
          <p className="mt-0.5 text-[11.5px] text-[#71717A]">
            {config.instrumentSymbol} · NSE · {config.timeframe}
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2.5">
          <span className="text-[12px] text-[#71717A]">
            Initial capital{' '}
            <strong className="font-semibold text-[#18181B] tabular-nums">
              {formatINR(config.initialCapital, { numberFormat })}
            </strong>
          </span>

          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="outline" size="sm" className="h-9 gap-2">
                <Calendar className="h-3.5 w-3.5 text-[#71717A]" />
                {config.dateRange}
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              {DATE_RANGES.map((range) => (
                <DropdownMenuItem
                  key={range}
                  onClick={() => setConfig((prev) => ({ ...prev, dateRange: range }))}
                >
                  {range}
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>

          <Button
            variant="outline"
            size="sm"
            onClick={() => setParamsOpen(true)}
            className="h-9 gap-1.5"
          >
            <SlidersHorizontal className="h-3.5 w-3.5 text-[#71717A]" />
            Parameters
          </Button>
        </div>
      </div>

      {/* Four KPI Summary Cards */}
      <QueryStateWrapper isLoading={isLoading} isError={isError} onRetry={() => refetch()}>
        {result && (
          <>
            <div className="grid grid-cols-1 gap-3.5 sm:grid-cols-2 xl:grid-cols-4">
              <KpiCard
                label="Net return"
                value={`${result.netReturnPct >= 0 ? '+' : '−'}${Math.abs(result.netReturnPct).toFixed(2)}%`}
                subtext={`${formatINR(result.netProfit, { numberFormat })} net profit`}
                tone={result.netReturnPct >= 0 ? 'positive' : 'negative'}
              />
              <KpiCard
                label="Max drawdown"
                value={`${result.maxDrawdownPct.toFixed(2)}%`}
                subtext="Peak-to-trough decline"
                tone="negative"
              />
              <KpiCard
                label="Sharpe ratio"
                value={result.sharpeRatio.toFixed(2)}
                subtext="Annualised · Risk-free 6%"
              />
              <KpiCard
                label="Win rate"
                value={`${result.winRatePct.toFixed(1)}%`}
                subtext={`${result.wins} wins / ${result.totalTrades} trades`}
              />
            </div>

            {/* Chart Selection Controls Row */}
            <div className="flex flex-wrap items-center justify-between gap-3 pt-1">
              <ChartToggleTabs value={activeTab} onChange={setActiveTab} />

              <div className="flex items-center gap-4">
                <span className="hidden text-[11.5px] text-[#71717A] sm:inline">
                  3 charts · Scroll to explore
                </span>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() =>
                    tradingPnlRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
                  }
                  className="h-8 gap-1.5 text-[12px]"
                >
                  Trading P&amp;L
                  <ArrowDown className="h-3.5 w-3.5 text-[#71717A]" />
                </Button>
              </div>
            </div>

            {/* 3 Vertically Stacked Graph Sections (ALL 3 ALWAYS PRESENT & SCROLLABLE) */}
            <div className="space-y-4">
              {/* Graph 1: Controlled by the 3 chart-selection tabs */}
              {activeTab === 'equity' && (
                <LineChartCard
                  title="Equity Curve"
                  subtitle={`Portfolio equity after costs · INR`}
                  data={result.series}
                  xKey="date"
                  series={[
                    {
                      dataKey: 'equity',
                      name: 'Strategy equity',
                      color: '#52647A',
                      strokeWidth: 1.9,
                    },
                  ]}
                  yFormatter={formatCompactLakhs}
                  tooltipFormatter={(v) => formatINR(v, { numberFormat })}
                  height={235}
                />
              )}

              {activeTab === 'comparison' && (
                <LineChartCard
                  title="Strategy vs Buy-and-Hold"
                  subtitle="Cumulative returns · Same start date and capital"
                  data={result.series}
                  xKey="date"
                  series={[
                    {
                      dataKey: 'strategyReturnPct',
                      name: config.strategy,
                      color: '#52647A',
                      strokeWidth: 1.9,
                    },
                    {
                      dataKey: 'buyHoldReturnPct',
                      name: 'Buy-and-hold',
                      color: '#9CA3AF',
                      strokeWidth: 1.5,
                    },
                  ]}
                  yFormatter={(v) => `${v}%`}
                  tooltipFormatter={(v) => `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`}
                  height={235}
                />
              )}

              {activeTab === 'sharpe' && (
                <LineChartCard
                  title="Rolling Sharpe Ratio"
                  subtitle="60-day rolling window · Annualised"
                  data={result.series}
                  xKey="date"
                  series={[
                    {
                      dataKey: 'rollingSharpe',
                      name: 'Rolling Sharpe',
                      color: '#52647A',
                      strokeWidth: 1.85,
                    },
                  ]}
                  yDomain={[0, 3]}
                  yFormatter={(v) => v.toFixed(2)}
                  tooltipFormatter={(v) => v.toFixed(2)}
                  height={235}
                />
              )}

              {/* Graph 2: Drawdown (Always rendered) */}
              <AreaDrawdownChart
                title="Drawdown"
                subtitle="Decline from previous equity peak · %"
                headerRight={
                  <span className="text-[12px] font-medium text-[#DC2626] tabular-nums">
                    Maximum {result.maxDrawdownPct.toFixed(2)}%
                  </span>
                }
                data={result.series}
                xKey="date"
                series={[
                  {
                    dataKey: 'drawdownPct',
                    name: 'Drawdown',
                    color: '#DC2626',
                    fillOpacity: 0.14,
                  },
                ]}
                height={195}
              />

              {/* Graph 3: Trading P&L (Always rendered & never clipped) */}
              <div ref={tradingPnlRef}>
                <PnlBarChart
                  title="Trading P&L"
                  subtitle="Monthly realised profit and loss · INR"
                  headerRight={
                    <span
                      className={cn(
                        'text-[12px] font-semibold tabular-nums',
                        result.netProfit >= 0 ? 'text-[#16A34A]' : 'text-[#DC2626]'
                      )}
                    >
                      Net {formatINR(result.netProfit, { showSign: true, numberFormat })}
                    </span>
                  }
                  data={result.monthlyPnl}
                  xKey="period"
                  valueKey="pnl"
                  height={210}
                />
              </div>
            </div>
          </>
        )}
      </QueryStateWrapper>

      <PageFooter leftText="Simulation only · Past performance does not guarantee future results." />

      {/* Saved Runs Modal */}
      <Dialog open={savedRunsOpen} onOpenChange={setSavedRunsOpen}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>Saved Backtest Runs</DialogTitle>
            <DialogDescription>
              Load any previously simulated run to compare parameters and curves.
            </DialogDescription>
          </DialogHeader>
          <div className="max-h-72 space-y-2 overflow-y-auto pr-1">
            {savedRuns.map((run) => (
              <button
                key={run.id}
                type="button"
                onClick={() => {
                  setSelectedInstrument(run.config.instrumentSymbol);
                  setConfig(run.config);
                  setSavedRunsOpen(false);
                }}
                className="flex w-full items-center justify-between rounded-lg border border-[#E4E4E7] p-3 text-left transition-colors hover:bg-[#F9FAFB]"
              >
                <div>
                  <p className="text-[13px] font-semibold text-[#18181B]">{run.name}</p>
                  <p className="mt-0.5 text-[11px] text-[#71717A]">
                    {run.config.dateRange} · {run.config.timeframe} · {run.createdAt}
                  </p>
                </div>
                <div className="text-right tabular-nums">
                  <p
                    className={cn(
                      'text-[13px] font-semibold',
                      run.netReturnPct >= 0 ? 'text-[#16A34A]' : 'text-[#DC2626]'
                    )}
                  >
                    +{run.netReturnPct.toFixed(2)}%
                  </p>
                  <p className="text-[11px] text-[#71717A]">Sharpe {run.sharpeRatio.toFixed(2)}</p>
                </div>
              </button>
            ))}
          </div>
        </DialogContent>
      </Dialog>

      {/* Parameters Modal */}
      <Dialog open={paramsOpen} onOpenChange={setParamsOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Backtest Parameters</DialogTitle>
            <DialogDescription>
              Configure capital, candle interval, stop loss, target, and slippage assumptions.
            </DialogDescription>
          </DialogHeader>
          <div className="grid grid-cols-2 gap-3.5 py-2">
            <div className="space-y-1.5">
              <Label>Initial Capital (₹)</Label>
              <Input
                type="number"
                value={config.initialCapital}
                onChange={(e) =>
                  setConfig((prev) => ({
                    ...prev,
                    initialCapital: Math.max(100000, Number(e.target.value) || 1000000),
                  }))
                }
              />
            </div>
            <div className="space-y-1.5">
              <Label>Candle Interval</Label>
              <Select
                value={config.timeframe}
                onValueChange={(val) => setConfig((prev) => ({ ...prev, timeframe: val }))}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {TIMEFRAME_OPTIONS.map((tf) => (
                    <SelectItem key={tf} value={tf}>
                      {tf}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label>Lookback Period</Label>
              <Input
                type="number"
                value={config.lookbackPeriod}
                onChange={(e) =>
                  setConfig((prev) => ({
                    ...prev,
                    lookbackPeriod: Math.max(5, Number(e.target.value) || 20),
                  }))
                }
              />
            </div>
            <div className="space-y-1.5">
              <Label>Slippage (%)</Label>
              <Input
                type="number"
                step="0.01"
                value={config.slippagePct}
                onChange={(e) =>
                  setConfig((prev) => ({
                    ...prev,
                    slippagePct: Math.max(0, Number(e.target.value) || 0.05),
                  }))
                }
              />
            </div>
            <div className="space-y-1.5">
              <Label>Stop Loss (%)</Label>
              <Input
                type="number"
                step="0.05"
                value={config.stopLossPct}
                onChange={(e) =>
                  setConfig((prev) => ({
                    ...prev,
                    stopLossPct: Math.max(0.1, Number(e.target.value) || 0.75),
                  }))
                }
              />
            </div>
            <div className="space-y-1.5">
              <Label>Target (%)</Label>
              <Input
                type="number"
                step="0.05"
                value={config.targetPct}
                onChange={(e) =>
                  setConfig((prev) => ({
                    ...prev,
                    targetPct: Math.max(0.2, Number(e.target.value) || 1.5),
                  }))
                }
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setParamsOpen(false)}>
              Close
            </Button>
            <Button
              onClick={() => {
                setParamsOpen(false);
                handleRunBacktest();
              }}
            >
              Apply &amp; Run Backtest
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
