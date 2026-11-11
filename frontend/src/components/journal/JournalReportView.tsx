import { Card } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { cn, formatINR, formatPrice } from '@/lib/utils';
import { useAppStore } from '@/store/useAppStore';
import type { JournalReport } from '@/types/trading';

interface JournalReportViewProps {
  report: JournalReport;
}

export function JournalReportView({ report }: JournalReportViewProps) {
  const numberFormat = useAppStore((s) => s.settings.regional.numberFormat);
  const isPositive = report.grossPnl >= 0;

  return (
    <Card className="p-6">
      {/* Top Label + Title */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <Badge variant="slate" className="text-[10px] font-semibold uppercase tracking-wider">
            AI-GENERATED ANALYSIS
          </Badge>
          <h2 className="mt-2.5 text-[20px] font-semibold tracking-tight text-[#18181B]">
            {report.instrument}
          </h2>
          <p className="mt-0.5 text-[12px] text-[#71717A]">
            {report.tradeId} · {report.dateLabel} · {report.strategy} · {report.direction}
          </p>
        </div>

        <Badge variant={report.status === 'Stopped' ? 'negative' : 'default'}>
          {report.status}
        </Badge>
      </div>

      {/* 4 Mini Summary KPI Boxes */}
      <div className="mt-5 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <div className="rounded-lg border border-[#E4E4E7] bg-[#F9FAFB]/70 p-3.5">
          <p className="text-[11px] text-[#71717A]">Gross P&amp;L</p>
          <p
            className={cn(
              'mt-1 text-[17px] font-semibold tabular-nums',
              isPositive ? 'text-[#16A34A]' : 'text-[#DC2626]'
            )}
          >
            {formatINR(report.grossPnl, { showSign: true, numberFormat })}
          </p>
        </div>
        <div className="rounded-lg border border-[#E4E4E7] bg-[#F9FAFB]/70 p-3.5">
          <p className="text-[11px] text-[#71717A]">Return on notional</p>
          <p
            className={cn(
              'mt-1 text-[17px] font-semibold tabular-nums',
              report.returnOnNotional >= 0 ? 'text-[#16A34A]' : 'text-[#DC2626]'
            )}
          >
            {report.returnOnNotional >= 0 ? '+' : '−'}
            {Math.abs(report.returnOnNotional).toFixed(2)}%
          </p>
        </div>
        <div className="rounded-lg border border-[#E4E4E7] bg-[#F9FAFB]/70 p-3.5">
          <p className="text-[11px] text-[#71717A]">Risk / reward</p>
          <p className="mt-1 text-[17px] font-semibold text-[#18181B] tabular-nums">
            {report.riskReward}
          </p>
        </div>
        <div className="rounded-lg border border-[#E4E4E7] bg-[#F9FAFB]/70 p-3.5">
          <p className="text-[11px] text-[#71717A]">Time in trade</p>
          <p className="mt-1 text-[17px] font-semibold text-[#18181B] tabular-nums">
            {report.timeInTrade}
          </p>
        </div>
      </div>

      {/* Trade thesis */}
      <section className="mt-6 border-b border-[#E4E4E7] pb-5">
        <h3 className="text-[13.5px] font-semibold text-[#18181B]">Trade thesis</h3>
        <p className="mt-2 text-[13px] leading-relaxed text-[#52525B]">{report.thesis}</p>
      </section>

      {/* Entry rationale & Exit explanation */}
      <section className="mt-5 grid grid-cols-1 gap-6 border-b border-[#E4E4E7] pb-5 sm:grid-cols-2">
        <div>
          <h3 className="text-[13.5px] font-semibold text-[#18181B]">Entry rationale</h3>
          <div className="mt-2 space-y-1 text-[12.5px] text-[#52525B] tabular-nums">
            <p>
              {report.entryTime} · ₹{formatPrice(report.entryPrice, 2, numberFormat)}
            </p>
            <p>
              {report.direction} · {report.quantity} units
            </p>
            <p>
              Stop ₹{formatPrice(report.stopPrice, 2, numberFormat)} · Risk{' '}
              {formatINR(report.plannedRisk, { numberFormat })}
            </p>
          </div>
        </div>

        <div>
          <h3 className="text-[13.5px] font-semibold text-[#18181B]">Exit explanation</h3>
          <div className="mt-2 space-y-1 text-[12.5px] text-[#52525B] tabular-nums">
            <p>
              {report.exitTime} · ₹{formatPrice(report.exitPrice, 2, numberFormat)}
            </p>
            <p>{report.exitReason}</p>
            <p>Planned target ₹{formatPrice(report.plannedTarget, 2, numberFormat)}</p>
          </div>
        </div>
      </section>

      {/* Performance review */}
      <section className="mt-5 border-b border-[#E4E4E7] pb-5">
        <h3 className="text-[13.5px] font-semibold text-[#18181B]">Performance review</h3>
        <p className="mt-2 text-[13px] leading-relaxed text-[#52525B]">
          {report.performanceReview}
        </p>
      </section>

      {/* Insights for the next trade */}
      <section className="mt-5">
        <h3 className="text-[13.5px] font-semibold text-[#18181B]">
          Insights for the next trade
        </h3>
        <div className="mt-3.5 space-y-4">
          {report.insights.map((insight) => (
            <div key={insight.index} className="flex items-start gap-4">
              <span className="mt-0.5 font-mono text-[11.5px] font-medium text-[#A1A1AA]">
                {insight.index}
              </span>
              <div>
                <p className="text-[13px] font-medium text-[#18181B]">{insight.title}</p>
                <p className="mt-0.5 text-[12px] text-[#71717A]">{insight.description}</p>
              </div>
            </div>
          ))}
        </div>
      </section>

      <p className="mt-6 border-t border-[#F4F4F5] pt-4 text-[11px] text-[#71717A]">
        Example AI analysis · Based on sample executions · Not investment advice
      </p>
    </Card>
  );
}
