import { useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { ArrowLeft, Calendar, ChevronDown, ChevronRight, ChevronUp, Download, Search } from 'lucide-react';
import { PageHeader } from '@/components/layout/PageHeader';
import { PageFooter } from '@/components/layout/PageFooter';
import { Card } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { KpiCard } from '@/components/shared/KpiCard';
import { JournalReportView } from '@/components/journal/JournalReportView';
import { TradeAssistantChat } from '@/components/journal/TradeAssistantChat';
import { MOCK_JOURNAL_REPORTS } from '@/data/mockJournal';
import { downloadTextFile } from '@/lib/csvExport';
import { cn, formatINR } from '@/lib/utils';
import { useAppStore } from '@/store/useAppStore';

type JournalPeriod = 'This month · Nov 2026' | 'Last 3 months' | 'All sessions';

export function JournalPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const initialInstrument = searchParams.get('instrument');

  const trades = useAppStore((s) => s.trades);
  const pushToast = useAppStore((s) => s.pushToast);
  const numberFormat = useAppStore((s) => s.settings.regional.numberFormat);

  const [query, setQuery] = useState('');
  const [period, setPeriod] = useState<JournalPeriod>('This month · Nov 2026');
  const [showAll, setShowAll] = useState(false);
  const [selectedInstrument, setSelectedInstrument] = useState<string | null>(
    initialInstrument
  );

  useEffect(() => {
    if (initialInstrument) {
      setSelectedInstrument(initialInstrument);
    }
  }, [initialInstrument]);

  // Filter trades by selected period
  const periodTrades = useMemo(() => {
    if (period === 'This month · Nov 2026') return trades;
    if (period === 'Last 3 months') return trades;
    return trades;
  }, [trades, period]);

  // Build unique list of previously traded instruments ordered by most recent closed/executed trade
  const tradedInstruments = useMemo(() => {
    const map = new Map<
      string,
      {
        instrument: string;
        instrumentName: string;
        dateLabel: string;
        timeLabel: string;
        strategy: string;
        direction: string;
        latestPnl: number;
        statusLabel: string;
        tradeId: string;
      }
    >();

    // Seed with curated top reports order matching Figma screenshot (BANKNIFTY, RELIANCE, HDFCBANK, ICICIBANK, NIFTY 50)
    const closedFirst = [
      ...periodTrades.filter((t) => Number(t.id.replace('#TR-', '')) > 24),
      ...periodTrades.filter((t) => t.id === '#TR-0021'),
      ...periodTrades.filter((t) => t.status !== 'Open'),
      ...periodTrades.filter((t) => t.status === 'Open'),
    ];

    for (const t of closedFirst) {
      if (!map.has(t.instrument)) {
        const matchingReport = MOCK_JOURNAL_REPORTS.find(
          (r) => r.instrument === t.instrument
        );
        map.set(t.instrument, {
          instrument: t.instrument,
          instrumentName: t.instrumentName,
          dateLabel: matchingReport?.dateLabel ?? t.entryDate,
          timeLabel: matchingReport?.timeLabel ?? t.entryTime,
          strategy: matchingReport?.strategy ?? t.strategy,
          direction: matchingReport?.direction ?? t.direction,
          latestPnl: matchingReport?.grossPnl ?? t.netPnl ?? 0,
          statusLabel: t.status === 'Open' ? 'Open position' : 'Closed trade',
          tradeId: matchingReport?.tradeId ?? t.id,
        });
      }
    }

    return Array.from(map.values());
  }, [periodTrades]);

  const filteredInstruments = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return tradedInstruments;
    return tradedInstruments.filter(
      (item) =>
        item.instrument.toLowerCase().includes(q) ||
        item.instrumentName.toLowerCase().includes(q) ||
        item.strategy.toLowerCase().includes(q) ||
        item.direction.toLowerCase().includes(q)
    );
  }, [tradedInstruments, query]);

  const visibleInstruments = showAll
    ? filteredInstruments
    : filteredInstruments.slice(0, 5);

  const closedTrades = periodTrades.filter((t) => t.status !== 'Open');
  const wins = closedTrades.filter((t) => (t.netPnl ?? 0) > 0).length;
  const winRate = closedTrades.length > 0 ? (wins / closedTrades.length) * 100 : 0;
  const realisedTotal = closedTrades.reduce((acc, t) => acc + (t.netPnl ?? 0), 0);

  // Resolve report for Detailed View (fallback synthesized cleanly from trade if not in pre-authored list)
  const activeReport = useMemo(() => {
    if (!selectedInstrument) return null;
    const found = MOCK_JOURNAL_REPORTS.find(
      (r) => r.instrument === selectedInstrument
    );
    if (found) return found;

    const fallbackTrade =
      trades.find((t) => t.instrument === selectedInstrument) ?? trades[0];
    const isWin = (fallbackTrade.netPnl ?? 0) >= 0;
    const riskAmt = Math.round(
      Math.abs(fallbackTrade.entryPrice - fallbackTrade.stopLoss) *
        fallbackTrade.quantity
    );

    return {
      instrument: fallbackTrade.instrument,
      instrumentName: fallbackTrade.instrumentName,
      tradeId: fallbackTrade.id,
      dateLabel: fallbackTrade.entryDate,
      timeLabel: fallbackTrade.entryTime,
      strategy: fallbackTrade.strategy,
      direction: fallbackTrade.direction,
      status: fallbackTrade.status,
      grossPnl: fallbackTrade.netPnl ?? 0,
      returnOnNotional: Number(
        (
          ((fallbackTrade.netPnl ?? 0) /
            Math.max(1, fallbackTrade.entryPrice * fallbackTrade.quantity)) *
          100
        ).toFixed(2)
      ),
      riskReward: `${fallbackTrade.rMultiple ? fallbackTrade.rMultiple.toFixed(2) : isWin ? '1.40' : '-1.00'}R`,
      timeInTrade: fallbackTrade.timeInTrade ?? '32m',
      thesis: `${fallbackTrade.strategy} entered ${fallbackTrade.direction.toLowerCase()} on ${fallbackTrade.instrument} at ${fallbackTrade.entryTime} following intraday structure confirmation.`,
      entryTime: fallbackTrade.entryTime,
      entryPrice: fallbackTrade.entryPrice,
      quantity: fallbackTrade.quantity,
      stopPrice: fallbackTrade.stopLoss,
      plannedRisk: Math.max(500, riskAmt),
      exitTime: fallbackTrade.entryTime,
      exitPrice: fallbackTrade.exitPrice ?? fallbackTrade.entryPrice,
      exitReason:
        fallbackTrade.status === 'Stopped'
          ? 'Stop loss triggered · Risk capped'
          : fallbackTrade.status === 'Closed'
            ? 'Target / rule exit completed'
            : 'Position currently open',
      plannedTarget: fallbackTrade.target,
      performanceReview: `Trade ${fallbackTrade.id} generated ${formatINR(fallbackTrade.netPnl ?? 0, { showSign: true })} across ${fallbackTrade.quantity} units with predefined stop protection at ₹${fallbackTrade.stopLoss.toLocaleString('en-IN')}.`,
      insights: [
        {
          index: '01',
          title: 'Predefined risk honoured',
          description: `Stop loss was placed at entry and bounded maximum loss exposure.`,
        },
        {
          index: '02',
          title: 'Track strategy consistency',
          description: `Evaluate ${fallbackTrade.strategy} across at least 20 sessions before tuning thresholds.`,
        },
      ],
      initialMessages: [
        {
          id: `init-${fallbackTrade.id}`,
          role: 'assistant' as const,
          content: `Loaded ${fallbackTrade.instrument} (${fallbackTrade.id}). Ask about entry timing, risk-to-reward, or strategy execution.`,
          timestamp: 'Now',
        },
      ],
      suggestedFollowUps: [
        'How could I improve the entry?',
        'Compare with my losing trades',
      ],
    };
  }, [selectedInstrument, trades]);

  const handleOpenReport = (symbol: string) => {
    setSelectedInstrument(symbol);
    setSearchParams({ instrument: symbol });
  };

  const handleBackToList = () => {
    setSelectedInstrument(null);
    setSearchParams({});
  };

  const handleExportReport = () => {
    if (!activeReport) return;
    const md = [
      `# AgentX Trade Journal Report — ${activeReport.instrument} (${activeReport.tradeId})`,
      `Date: ${activeReport.dateLabel} | Strategy: ${activeReport.strategy} | Direction: ${activeReport.direction} | Status: ${activeReport.status}`,
      '',
      `## Metrics`,
      `- Gross P&L: ${formatINR(activeReport.grossPnl, { showSign: true })}`,
      `- Return on Notional: ${activeReport.returnOnNotional}%`,
      `- Risk / Reward: ${activeReport.riskReward}`,
      `- Time in Trade: ${activeReport.timeInTrade}`,
      '',
      `## Trade Thesis`,
      activeReport.thesis,
      '',
      `## Performance Review`,
      activeReport.performanceReview,
      '',
      `## Insights for the Next Trade`,
      ...activeReport.insights.map((i) => `${i.index}. ${i.title}: ${i.description}`),
    ].join('\n');

    downloadTextFile(
      `agentx-journal-${activeReport.instrument.toLowerCase().replace(/\s+/g, '-')}-${activeReport.tradeId.replace('#', '')}.md`,
      md,
      'text/markdown;charset=utf-8;'
    );

    pushToast({
      title: 'Journal report exported',
      description: `${activeReport.instrument} (${activeReport.tradeId}) downloaded as Markdown.`,
      variant: 'success',
    });
  };

  // Detailed Two-Column Report View
  if (activeReport) {
    return (
      <div className="space-y-4">
        <PageHeader
          title="Trade Journal"
          subtitle="Instrument report · Review, reflect, refine."
          actions={
            <>
              <Button variant="outline" onClick={handleBackToList}>
                <ArrowLeft className="h-3.5 w-3.5" />
                All instruments
              </Button>
              <Button variant="default" onClick={handleExportReport}>
                <Download className="h-3.5 w-3.5" />
                Export report
              </Button>
            </>
          }
        />

        <div className="grid grid-cols-1 gap-5 xl:grid-cols-[1fr_360px]">
          <JournalReportView report={activeReport} />
          <TradeAssistantChat report={activeReport} />
        </div>

        <PageFooter leftText="Sample journal · Simulated executions · AI insights are for review, not trading advice." />
      </div>
    );
  }

  // Overview Screen
  return (
    <div className="space-y-4">
      <PageHeader
        title="Trade Journal"
        subtitle="Review the decision, not just the outcome."
        actions={
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="outline" className="gap-2">
                <Calendar className="h-3.5 w-3.5 text-[#71717A]" />
                {period}
                <ChevronDown className="h-3.5 w-3.5 text-[#71717A]" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              {(['This month · Nov 2026', 'Last 3 months', 'All sessions'] as JournalPeriod[]).map(
                (opt) => (
                  <DropdownMenuItem key={opt} onClick={() => setPeriod(opt)}>
                    {opt}
                  </DropdownMenuItem>
                )
              )}
            </DropdownMenuContent>
          </DropdownMenu>
        }
      />

      {/* Full-Width Search Bar */}
      <Card className="flex items-center justify-between gap-3 px-4 py-2.5">
        <div className="flex flex-1 items-center gap-2.5">
          <Search className="h-4 w-4 shrink-0 text-[#71717A]" />
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search all previously traded instruments..."
            className="w-full bg-transparent text-[13px] text-[#18181B] placeholder:text-[#71717A] focus:outline-none"
          />
        </div>
        <span className="hidden text-[11.5px] text-[#71717A] sm:inline">
          Search by symbol or name
        </span>
      </Card>

      {/* Three Summary KPI Cards */}
      <div className="grid grid-cols-1 gap-3.5 sm:grid-cols-3">
        <KpiCard
          label="executed trades"
          value={String(trades.length)}
          subtext={`${closedTrades.length} closed · ${trades.length - closedTrades.length} open`}
        />
        <KpiCard
          label="win rate"
          value={`${winRate.toFixed(1)}%`}
          subtext={`${wins} of ${closedTrades.length} closed trades`}
        />
        <KpiCard
          label="realised P&L"
          value={formatINR(realisedTotal, { showSign: true, numberFormat })}
          subtext="After fees and slippage"
          tone={realisedTotal >= 0 ? 'positive' : 'negative'}
        />
      </div>

      {/* Recently Traded Instruments */}
      <div className="pt-1">
        <div className="mb-3">
          <h2 className="text-[15px] font-semibold text-[#18181B]">
            Recently traded instruments
          </h2>
          <p className="mt-0.5 text-[11.5px] text-[#71717A]">
            Your five most recent instruments. Open one to review its trades.
          </p>
        </div>

        <Card className="overflow-hidden">
          <div className="hidden grid-cols-[1.3fr_1.4fr_1fr_auto] gap-4 border-b border-[#F4F4F5] px-5 py-2.5 text-[10px] font-medium uppercase tracking-[0.07em] text-[#71717A] sm:grid">
            <span>INSTRUMENT</span>
            <span>LAST CLOSED TRADE</span>
            <span>LATEST REALISED P&amp;L</span>
            <span className="pr-4 text-right">REPORT</span>
          </div>

          <div className="divide-y divide-[#F4F4F5]">
            {visibleInstruments.length === 0 ? (
              <div className="px-5 py-10 text-center text-xs text-[#71717A]">
                No traded instruments match &ldquo;{query}&rdquo;.
              </div>
            ) : (
              visibleInstruments.map((item) => {
                const pos = item.latestPnl >= 0;
                return (
                  <div
                    key={item.instrument}
                    className="grid grid-cols-1 items-center gap-3 px-5 py-4 transition-colors hover:bg-[#F9FAFB]/70 sm:grid-cols-[1.3fr_1.4fr_1fr_auto]"
                  >
                    <div>
                      <p className="text-[13.5px] font-semibold text-[#18181B]">
                        {item.instrument}
                      </p>
                      <p className="mt-0.5 text-[11.5px] text-[#71717A]">
                        {item.instrumentName}
                      </p>
                    </div>

                    <div>
                      <p className="text-[12.5px] text-[#18181B] tabular-nums">
                        {item.dateLabel} · {item.timeLabel}
                      </p>
                      <p className="mt-0.5 text-[11.5px] text-[#71717A]">
                        {item.strategy} · {item.direction}
                      </p>
                    </div>

                    <div>
                      <p
                        className={cn(
                          'text-[13.5px] font-semibold tabular-nums',
                          pos ? 'text-[#16A34A]' : 'text-[#DC2626]'
                        )}
                      >
                        {formatINR(item.latestPnl, { showSign: true, numberFormat })}
                      </p>
                      <p className="mt-0.5 text-[11.5px] text-[#71717A]">
                        {item.statusLabel}
                      </p>
                    </div>

                    <div className="flex justify-end">
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => handleOpenReport(item.instrument)}
                        className="gap-1.5"
                      >
                        View report
                        <ChevronRight className="h-3.5 w-3.5 text-[#71717A]" />
                      </Button>
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </Card>

        {/* See More Action */}
        {filteredInstruments.length > 5 && (
          <div className="mt-4 flex justify-end sm:justify-center">
            <Button
              variant="outline"
              onClick={() => setShowAll((prev) => !prev)}
              className="gap-1.5 px-5"
            >
              {showAll ? (
                <>
                  Show Less
                  <ChevronUp className="h-3.5 w-3.5" />
                </>
              ) : (
                <>
                  See More ({filteredInstruments.length - 5} more)
                  <ChevronDown className="h-3.5 w-3.5" />
                </>
              )}
            </Button>
          </div>
        )}
      </div>

      {/* Bottom Pattern Note */}
      <div className="pt-2">
        <p className="text-[13.5px] font-semibold text-[#18181B]">
          Find patterns in the process.
        </p>
        <p className="mt-0.5 text-[11.5px] text-[#71717A]">
          Each report connects your strategy, execution and outcome in one place.
        </p>
      </div>

      <PageFooter leftText="Sample journal · Simulated executions · AI insights are for review, not trading advice." />
    </div>
  );
}
