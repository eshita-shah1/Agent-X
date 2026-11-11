import { ChevronDown } from 'lucide-react';
import { PageHeader } from '@/components/layout/PageHeader';
import { PageFooter } from '@/components/layout/PageFooter';
import { Badge } from '@/components/ui/badge';
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { CandlestickChart } from '@/components/charts/CandlestickChart';
import { OrderTicket } from '@/components/trading/OrderTicket';
import { PositionsPanel } from '@/components/trading/PositionsPanel';
import { cn, formatPrice } from '@/lib/utils';
import { useAppStore } from '@/store/useAppStore';
import { useInstrumentsQuery } from '@/hooks/useTradingQueries';

export function TradingPage() {
  const selectedSymbol = useAppStore((s) => s.selectedInstrumentSymbol);
  const setSelectedInstrument = useAppStore((s) => s.setSelectedInstrument);
  const numberFormat = useAppStore((s) => s.settings.regional.numberFormat);
  const { data: instruments = [] } = useInstrumentsQuery();

  const instrument =
    instruments.find((i) => i.symbol === selectedSymbol) ?? instruments[1];

  if (!instrument) return null;

  const isPositive = instrument.changePercent >= 0;

  return (
    <div className="space-y-4">
      <PageHeader
        title={`${instrument.symbol} · ${instrument.name}`}
        subtitle={`${instrument.exchange} · Open ₹${formatPrice(instrument.open, 2, numberFormat)} · High ₹${formatPrice(instrument.high, 2, numberFormat)} · Low ₹${formatPrice(instrument.low, 2, numberFormat)} · Prev Close ₹${formatPrice(instrument.prevClose, 2, numberFormat)}`}
        actions={
          <div className="flex flex-wrap items-center gap-2.5">
            <div className="hidden items-baseline gap-2 rounded-lg border border-[#E4E4E7] bg-white px-3 py-1.5 sm:flex tabular-nums">
              <span className="text-[14px] font-semibold text-[#18181B]">
                ₹{formatPrice(instrument.lastPrice, 2, numberFormat)}
              </span>
              <span
                className={cn(
                  'text-[11.5px] font-medium',
                  isPositive ? 'text-[#16A34A]' : 'text-[#DC2626]'
                )}
              >
                {isPositive ? '+' : '−'}
                {Math.abs(instrument.changePercent).toFixed(2)}%
              </span>
            </div>

            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <button
                  type="button"
                  className="inline-flex h-9 items-center gap-2 rounded-lg border border-[#E4E4E7] bg-white px-3 text-[13px] font-medium text-[#18181B] hover:bg-[#F9FAFB]"
                >
                  <span>Switch instrument</span>
                  <ChevronDown className="h-3.5 w-3.5 text-[#71717A]" />
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="max-h-72 w-60 overflow-y-auto">
                <DropdownMenuLabel>NSE / BSE Instruments</DropdownMenuLabel>
                <DropdownMenuSeparator />
                {instruments.map((item) => (
                  <DropdownMenuCheckboxItem
                    key={item.symbol}
                    checked={item.symbol === instrument.symbol}
                    onCheckedChange={() => setSelectedInstrument(item.symbol)}
                  >
                    <span className="flex w-full items-center justify-between gap-2">
                      <span className="font-medium">{item.symbol}</span>
                      <span className="text-[11px] text-[#71717A] tabular-nums">
                        ₹{formatPrice(item.lastPrice, 2, numberFormat)}
                      </span>
                    </span>
                  </DropdownMenuCheckboxItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>

            <Badge variant="slate" className="h-8 px-3 text-[11px] font-semibold tracking-wider">
              PAPER ACCOUNT
            </Badge>
          </div>
        }
      />

      {/* Expanded Workspace Grid: Large Candlestick Chart + Validated Order Ticket */}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[1fr_365px]">
        <CandlestickChart
          height={435}
          subtitleTag="Manual paper execution"
        />
        <OrderTicket instrument={instrument} />
      </div>

      {/* Active Paper Positions & Manual Order Blotter */}
      <PositionsPanel />

      <PageFooter />
    </div>
  );
}
