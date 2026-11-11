import { useState } from 'react';
import { PageHeader } from '@/components/layout/PageHeader';
import { PageFooter } from '@/components/layout/PageFooter';
import { Badge } from '@/components/ui/badge';
import { KpiCard } from '@/components/shared/KpiCard';
import { TimeframeSelector } from '@/components/shared/TimeframeSelector';
import { QueryStateWrapper } from '@/components/shared/QueryStateWrapper';
import { LineChartCard } from '@/components/charts/LineChartCard';
import { HoldingPerformanceChart } from '@/components/charts/HoldingPerformanceChart';
import { StockAllocationDonut } from '@/components/charts/StockAllocationDonut';
import { cn, formatCompactLakhs, formatINR } from '@/lib/utils';
import { useAppStore } from '@/store/useAppStore';
import { usePortfolioQuery } from '@/hooks/useTradingQueries';
import type { PortfolioTimeframe } from '@/types/trading';

const PORTFOLIO_TIMEFRAMES: PortfolioTimeframe[] = ['1D', '1W', '1M', '3M', '6M', '1Y', 'All'];

export function PortfolioPage() {
  const [timeframe, setTimeframe] = useState<PortfolioTimeframe>('6M');
  const realisedPnl = useAppStore((s) => s.realisedPnl);
  const availableBalance = useAppStore((s) => s.availableBalance);
  const numberFormat = useAppStore((s) => s.settings.regional.numberFormat);

  const { data, isLoading, isError, refetch } = usePortfolioQuery(timeframe);
  const holdings = data?.holdings ?? [];
  const series = data?.series;

  const investedCapital = holdings.reduce((acc, h) => acc + h.investedValue, 0) || 800000;
  const holdingsValue = holdings.reduce((acc, h) => acc + h.currentValue, 0) || 838420;
  const unrealisedPnl = holdingsValue - investedCapital;
  const totalPortfolioValue = holdingsValue + availableBalance;
  const unrealisedPct = (unrealisedPnl / investedCapital) * 100;

  return (
    <div className="space-y-4">
      <PageHeader
        title="Portfolio"
        subtitle="A clear view of your paper capital."
        actions={
          <Badge variant="slate" className="h-8 px-3.5 text-[11px] font-semibold tracking-wider">
            PAPER ACCOUNT
          </Badge>
        }
      />

      {/* Five Top KPI Cards */}
      <div className="grid grid-cols-1 gap-3.5 sm:grid-cols-2 lg:grid-cols-5">
        <KpiCard
          label="Realised P&L"
          value={formatINR(realisedPnl, { showSign: true, numberFormat })}
          subtext="Closed positions"
          tone={realisedPnl >= 0 ? 'positive' : 'negative'}
        />
        <KpiCard
          label="Unrealised P&L"
          value={formatINR(unrealisedPnl, { showSign: true, numberFormat })}
          subtext={`${unrealisedPct >= 0 ? '+' : '−'}${Math.abs(unrealisedPct).toFixed(2)}% on invested capital`}
          tone={unrealisedPnl >= 0 ? 'positive' : 'negative'}
        />
        <KpiCard
          label="Total Portfolio Value"
          value={formatINR(totalPortfolioValue, { numberFormat })}
          subtext="Holdings + available balance"
        />
        <KpiCard
          label="Invested Capital"
          value={formatINR(investedCapital, { numberFormat })}
          subtext={`Cost basis · ${holdings.length} holdings`}
        />
        <KpiCard
          label="Available Balance"
          value={formatINR(availableBalance, { numberFormat })}
          subtext="Ready to deploy"
        />
      </div>

      {/* Main Portfolio Performance Line Chart */}
      <QueryStateWrapper isLoading={isLoading} isError={isError} onRetry={() => refetch()}>
        {series && (
          <LineChartCard
            title="Portfolio performance"
            subtitle="Total account value · INR"
            showLegend={false}
            headerMetric={
              <div className="flex flex-wrap items-baseline gap-2.5 tabular-nums">
                <span className="text-[22px] font-semibold tracking-tight text-[#18181B]">
                  {formatINR(totalPortfolioValue, { numberFormat })}
                </span>
                <span
                  className={cn(
                    'text-[12.5px] font-medium',
                    series.changeValue >= 0 ? 'text-[#16A34A]' : 'text-[#DC2626]'
                  )}
                >
                  {formatINR(series.changeValue, { showSign: true, numberFormat })} (
                  {series.changePercent >= 0 ? '+' : '−'}
                  {Math.abs(series.changePercent).toFixed(2)}%)
                </span>
              </div>
            }
            headerRight={
              <TimeframeSelector
                options={PORTFOLIO_TIMEFRAMES}
                value={timeframe}
                onChange={setTimeframe}
              />
            }
            data={series.points}
            xKey="label"
            series={[
              {
                dataKey: 'value',
                name: 'Account value',
                color: '#52647A',
                strokeWidth: 1.9,
              },
            ]}
            yFormatter={formatCompactLakhs}
            tooltipFormatter={(val) => formatINR(val, { numberFormat })}
            height={250}
          />
        )}
      </QueryStateWrapper>

      {/* Bottom Side-by-Side Stock Holdings Charts */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <HoldingPerformanceChart holdings={holdings} numberFormat={numberFormat} />
        <StockAllocationDonut holdings={holdings} numberFormat={numberFormat} />
      </div>

      <PageFooter leftText="Sample portfolio · Virtual capital only · All values in INR" />
    </div>
  );
}
