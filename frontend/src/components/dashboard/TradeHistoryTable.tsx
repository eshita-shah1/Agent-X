import { useMemo, useState } from 'react';
import { Calendar, ChevronDown, ChevronLeft, ChevronRight, Eye } from 'lucide-react';
import { Card } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { DirectionBadge, TradeStatusBadge } from '@/components/shared/StatusBadge';
import { cn, formatINR, formatPrice } from '@/lib/utils';
import { useAppStore } from '@/store/useAppStore';
import type { AgentTrade } from '@/types/trading';

type TabFilter = 'all' | 'open' | 'closed';
type DateFilter = 'today' | 'week' | 'month' | 'all';

const DATE_LABELS: Record<DateFilter, string> = {
  today: 'Today',
  week: 'Last 7 days',
  month: 'This month',
  all: 'All sessions',
};

const PAGE_SIZE = 5;

interface TradeHistoryTableProps {
  onSelectTrade: (trade: AgentTrade) => void;
}

export function TradeHistoryTable({ onSelectTrade }: TradeHistoryTableProps) {
  const trades = useAppStore((s) => s.trades);
  const numberFormat = useAppStore((s) => s.settings.regional.numberFormat);

  const [tab, setTab] = useState<TabFilter>('all');
  const [dateFilter, setDateFilter] = useState<DateFilter>('today');
  const [page, setPage] = useState(1);

  const openCount = useMemo(
    () => trades.filter((t) => t.status === 'Open').length,
    [trades]
  );
  const closedCount = trades.length - openCount;

  const filteredTrades = useMemo(() => {
    return trades.filter((trade) => {
      if (tab === 'open' && trade.status !== 'Open') return false;
      if (tab === 'closed' && trade.status === 'Open') return false;

      if (dateFilter === 'today') return true; // Keep initial 24 visible in Today view for pagination parity unless filtered
      if (dateFilter === 'week') return trade.dateBucket === 'today' || trade.dateBucket === 'week';
      return true;
    });
  }, [trades, tab, dateFilter]);

  const totalPages = Math.max(1, Math.ceil(filteredTrades.length / PAGE_SIZE));
  const safePage = Math.min(page, totalPages);
  const pageRows = filteredTrades.slice((safePage - 1) * PAGE_SIZE, safePage * PAGE_SIZE);


  return (
    <Card className="p-5">
      {/* Section Header */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-[15px] font-semibold text-[#18181B]">
            Agent activity <span className="mx-1.5 font-normal text-[#A1A1AA]">/</span>{' '}
            <span className="font-normal text-[#71717A]">Trade history</span>
          </h2>
          <p className="mt-0.5 text-[11.5px] text-[#71717A]">
            Demo data generated in the browser: these are not agent records or simulated fills.
          </p>
        </div>

        <div className="inline-flex items-center gap-2 rounded-full border border-[#E4E4E7] bg-[#F9FAFB] px-3 py-1 text-[11.5px] text-[#52525B]">
          <span className="h-2 w-2 rounded-full bg-[#F59E0B]" />
          <span>
            DEMO DATA · {openCount} mock position{openCount === 1 ? '' : 's'}
          </span>
        </div>
      </div>

      {/* Filter Pills + Date Filter */}
      <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => {
              setTab('all');
              setPage(1);
            }}
            className={cn(
              'inline-flex items-center gap-2 rounded-lg border px-3 py-1.5 text-[12px] font-medium transition-colors',
              tab === 'all'
                ? 'border-[#D4D4D8] bg-[#F1F5F9] text-[#18181B]'
                : 'border-[#E4E4E7] bg-white text-[#52525B] hover:bg-[#F9FAFB]'
            )}
          >
            <span>All Actions</span>
            <span className="text-[#71717A] tabular-nums">{trades.length}</span>
          </button>

          <button
            type="button"
            onClick={() => {
              setTab('open');
              setPage(1);
            }}
            className={cn(
              'inline-flex items-center gap-2 rounded-lg border px-3 py-1.5 text-[12px] font-medium transition-colors',
              tab === 'open'
                ? 'border-[#D4D4D8] bg-[#F1F5F9] text-[#18181B]'
                : 'border-[#E4E4E7] bg-white text-[#52525B] hover:bg-[#F9FAFB]'
            )}
          >
            <span>Open Positions</span>
            <span className="text-[#71717A] tabular-nums">{openCount}</span>
          </button>

          <button
            type="button"
            onClick={() => {
              setTab('closed');
              setPage(1);
            }}
            className={cn(
              'inline-flex items-center gap-2 rounded-lg border px-3 py-1.5 text-[12px] font-medium transition-colors',
              tab === 'closed'
                ? 'border-[#D4D4D8] bg-[#F1F5F9] text-[#18181B]'
                : 'border-[#E4E4E7] bg-white text-[#52525B] hover:bg-[#F9FAFB]'
            )}
          >
            <span>Closed Positions</span>
            <span className="text-[#71717A] tabular-nums">{closedCount}</span>
          </button>
        </div>

        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="outline" size="sm" className="h-8 gap-2 text-[12px]">
              <Calendar className="h-3.5 w-3.5 text-[#71717A]" />
              {DATE_LABELS[dateFilter]}
              <ChevronDown className="h-3.5 w-3.5 text-[#71717A]" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            {(Object.keys(DATE_LABELS) as DateFilter[]).map((key) => (
              <DropdownMenuItem
                key={key}
                onClick={() => {
                  setDateFilter(key);
                  setPage(1);
                }}
              >
                {DATE_LABELS[key]}
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      {/* 13-Column Horizontally Scrollable Table (Exact Column Order) */}
      <div className="mt-4 overflow-hidden rounded-lg border border-[#E4E4E7]">
        <Table>
          <TableHeader>
            <TableRow className="hover:bg-[#F9FAFB]">
              <TableHead>Trade ID</TableHead>
              <TableHead>Instrument</TableHead>
              <TableHead>Direction</TableHead>
              <TableHead>Strategy</TableHead>
              <TableHead>Entry Time</TableHead>
              <TableHead className="text-right">Entry Price</TableHead>
              <TableHead className="text-right">Quantity</TableHead>
              <TableHead className="text-right">Stop Loss</TableHead>
              <TableHead className="text-right">Target</TableHead>
              <TableHead className="text-right">Exit Price</TableHead>
              <TableHead className="text-right">Net P&amp;L</TableHead>
              <TableHead>Status</TableHead>
              <TableHead className="text-right">Action</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {pageRows.length === 0 ? (
              <TableRow>
                <TableCell colSpan={13} className="py-10 text-center text-xs text-[#71717A]">
                  No trades match the current filter.
                </TableCell>
              </TableRow>
            ) : (
              pageRows.map((trade) => {
                const pnl = trade.netPnl ?? 0;
                const pnlPositive = pnl >= 0;

                return (
                  <TableRow key={trade.id}>
                    <TableCell className="font-mono text-[12px] text-[#71717A]">
                      {trade.id}
                    </TableCell>
                    <TableCell className="font-semibold text-[#18181B]">
                      {trade.instrument}
                    </TableCell>
                    <TableCell>
                      <DirectionBadge direction={trade.direction} />
                    </TableCell>
                    <TableCell className="text-[#52525B]">{trade.strategy}</TableCell>
                    <TableCell className="text-[#71717A] tabular-nums">
                      {trade.entryTime}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {formatPrice(trade.entryPrice, 2, numberFormat)}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {trade.quantity}
                    </TableCell>
                    <TableCell className="text-right text-[#52525B] tabular-nums">
                      {formatPrice(trade.stopLoss, 2, numberFormat)}
                    </TableCell>
                    <TableCell className="text-right text-[#52525B] tabular-nums">
                      {formatPrice(trade.target, 2, numberFormat)}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {trade.exitPrice === null ? (
                        <span className="font-medium text-[#52647A]">Open</span>
                      ) : (
                        formatPrice(trade.exitPrice, 2, numberFormat)
                      )}
                    </TableCell>
                    <TableCell
                      className={cn(
                        'text-right font-medium tabular-nums',
                        trade.netPnl === null
                          ? 'text-[#71717A]'
                          : pnlPositive
                            ? 'text-[#16A34A]'
                            : 'text-[#DC2626]'
                      )}
                    >
                      {trade.netPnl === null
                        ? '—'
                        : formatINR(pnl, { showSign: true, numberFormat })}
                    </TableCell>
                    <TableCell>
                      <TradeStatusBadge status={trade.status} />
                    </TableCell>
                    <TableCell className="text-right">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => onSelectTrade(trade)}
                        className="h-7 gap-1 px-2 text-[12px] font-medium text-[#52647A] hover:text-[#18181B]"
                      >
                        <Eye className="h-3.5 w-3.5" />
                        View Details
                      </Button>
                    </TableCell>
                  </TableRow>
                );
              })
            )}
          </TableBody>
        </Table>
      </div>

      {/* Pagination Footer */}
      <div className="mt-3.5 flex flex-wrap items-center justify-between gap-2 text-[11.5px] text-[#71717A]">
        <span>
          Showing {pageRows.length} of {filteredTrades.length} trades
        </span>

        <div className="flex items-center gap-1">
          <button
            type="button"
            disabled={safePage <= 1}
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            className="rounded-md p-1.5 text-[#71717A] transition-colors hover:bg-[#F4F4F5] disabled:opacity-40"
            aria-label="Previous page"
          >
            <ChevronLeft className="h-3.5 w-3.5" />
          </button>

          {Array.from({ length: totalPages }, (_, i) => i + 1).map((num) => (
            <button
              key={num}
              type="button"
              onClick={() => setPage(num)}
              className={cn(
                'h-7 min-w-[28px] rounded-md px-2 text-[12px] font-medium transition-colors',
                num === safePage
                  ? 'border border-[#E4E4E7] bg-[#F4F4F5] text-[#18181B]'
                  : 'text-[#71717A] hover:bg-[#F9FAFB]'
              )}
            >
              {num}
            </button>
          ))}

          <button
            type="button"
            disabled={safePage >= totalPages}
            onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
            className="rounded-md p-1.5 text-[#71717A] transition-colors hover:bg-[#F4F4F5] disabled:opacity-40"
            aria-label="Next page"
          >
            <ChevronRight className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>
    </Card>
  );
}
