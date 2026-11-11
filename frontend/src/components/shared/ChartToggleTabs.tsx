import { cn } from '@/lib/utils';
import type { BacktestChartTab } from '@/types/trading';

const CHART_TABS: readonly { id: BacktestChartTab; label: string }[] = [
  { id: 'equity', label: 'Equity Curve' },
  { id: 'comparison', label: 'Strategy vs Buy-and-Hold' },
  { id: 'sharpe', label: 'Rolling Sharpe Ratio' },
];

interface ChartToggleTabsProps {
  value: BacktestChartTab;
  onChange: (tab: BacktestChartTab) => void;
  className?: string;
}

export function ChartToggleTabs({ value, onChange, className }: ChartToggleTabsProps) {
  return (
    <div className={cn('flex flex-wrap items-center gap-2', className)}>
      {CHART_TABS.map((tab) => {
        const active = value === tab.id;
        return (
          <button
            key={tab.id}
            type="button"
            onClick={() => onChange(tab.id)}
            className={cn(
              'rounded-full border px-4 py-1.5 text-[12.5px] font-medium transition-colors',
              active
                ? 'border-[#D4D4D8] bg-[#F1F5F9] font-semibold text-[#18181B]'
                : 'border-[#E4E4E7] bg-white text-[#52525B] hover:text-[#18181B]'
            )}
          >
            {tab.label}
          </button>
        );
      })}
    </div>
  );
}
