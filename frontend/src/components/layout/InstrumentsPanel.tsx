import { useMemo, useState } from 'react';
import { Search } from 'lucide-react';
import { cn, formatPrice } from '@/lib/utils';
import { useAppStore } from '@/store/useAppStore';
import { useInstrumentsQuery } from '@/hooks/useTradingQueries';
import type { Instrument } from '@/types/trading';

interface InstrumentsPanelProps {
  onSelectInstrument?: (symbol: string) => void;
  className?: string;
}

export function InstrumentsPanel({ onSelectInstrument, className }: InstrumentsPanelProps) {
  const [query, setQuery] = useState('');
  const selectedSymbol = useAppStore((s) => s.selectedInstrumentSymbol);
  const setSelectedInstrument = useAppStore((s) => s.setSelectedInstrument);
  const numberFormat = useAppStore((s) => s.settings.regional.numberFormat);
  const { data: instruments = [] } = useInstrumentsQuery();

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return instruments;
    return instruments.filter(
      (item) =>
        item.symbol.toLowerCase().includes(q) ||
        item.name.toLowerCase().includes(q)
    );
  }, [instruments, query]);

  const indices = filtered.filter((i) => i.category === 'INDEX');
  const stocks = filtered.filter((i) => i.category === 'STOCK');

  const handleSelect = (symbol: string) => {
    setSelectedInstrument(symbol);
    onSelectInstrument?.(symbol);
  };

  const renderRow = (item: Instrument) => {
    const isSelected = item.symbol === selectedSymbol;
    const isPositive = item.changePercent >= 0;

    return (
      <button
        key={item.symbol}
        type="button"
        onClick={() => handleSelect(item.symbol)}
        className={cn(
          'relative flex w-full items-center justify-between px-4 py-2.5 text-left transition-colors',
          isSelected ? 'bg-[#F1F5F9]' : 'hover:bg-[#F9FAFB]'
        )}
      >
        {isSelected && (
          <span className="absolute left-0 top-1.5 bottom-1.5 w-[3px] rounded-r-full bg-[#52647A]" />
        )}
        <div className="min-w-0 pr-2">
          <p className="truncate text-[12.5px] font-semibold text-[#18181B]">
            {item.symbol}
          </p>
          <p className="truncate text-[11px] text-[#71717A]">{item.name}</p>
        </div>
        <div className="shrink-0 text-right tabular-nums">
          <p className="text-[12.5px] font-semibold text-[#18181B]">
            {formatPrice(item.lastPrice, 2, numberFormat)}
          </p>
          <p
            className={cn(
              'text-[11px] font-medium',
              isPositive ? 'text-[#16A34A]' : 'text-[#DC2626]'
            )}
          >
            {isPositive ? '+' : '−'}
            {Math.abs(item.changePercent).toFixed(2)}%
          </p>
        </div>
      </button>
    );
  };

  return (
    <aside
      className={cn(
        'flex h-full w-[288px] shrink-0 flex-col border-l border-[#E4E4E7] bg-white',
        className
      )}
      aria-label="Instruments selector"
    >
      {/* Panel Header */}
      <div className="px-4 pt-5 pb-3">
        <h2 className="text-[15px] font-semibold text-[#18181B]">Instruments</h2>
        <p className="mt-0.5 text-[11.5px] text-[#71717A]">NSE · Sample session</p>

        <div className="relative mt-3">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[#71717A]" />
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search instruments"
            className="h-9 w-full rounded-lg border border-[#E4E4E7] bg-[#F9FAFB] pl-8 pr-3 text-[12.5px] text-[#18181B] placeholder:text-[#71717A] focus:border-[#52647A] focus:bg-white focus:outline-none"
          />
        </div>

        <div className="mt-3.5 flex items-center justify-between text-[10px] font-medium uppercase tracking-[0.06em] text-[#71717A]">
          <span>SYMBOL</span>
          <span>LAST / CHANGE</span>
        </div>
      </div>

      {/* Scrollable Instrument List */}
      <div className="flex-1 overflow-y-auto border-t border-[#F4F4F5]">
        {indices.length > 0 && (
          <div className="py-1.5">
            <p className="px-4 py-1.5 text-[10px] font-semibold uppercase tracking-[0.08em] text-[#71717A]">
              INDICES
            </p>
            <div className="divide-y divide-[#F4F4F5]">{indices.map(renderRow)}</div>
          </div>
        )}

        {stocks.length > 0 && (
          <div className="border-t border-[#F4F4F5] py-1.5">
            <p className="px-4 py-1.5 text-[10px] font-semibold uppercase tracking-[0.08em] text-[#71717A]">
              STOCKS
            </p>
            <div className="divide-y divide-[#F4F4F5]">{stocks.map(renderRow)}</div>
          </div>
        )}

        {indices.length === 0 && stocks.length === 0 && (
          <div className="px-4 py-8 text-center text-xs text-[#71717A]">
            No instruments match &ldquo;{query}&rdquo;.
          </div>
        )}
      </div>

      {/* Panel Footer */}
      <div className="border-t border-[#E4E4E7] px-4 py-3 text-[11px] leading-relaxed text-[#71717A]">
        <p>Select an instrument to view its chart.</p>
        <p>Prices shown in INR</p>
      </div>
    </aside>
  );
}
