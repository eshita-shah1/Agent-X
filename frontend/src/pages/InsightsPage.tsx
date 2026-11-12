import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { AlertTriangle, LogOut, Sparkles } from 'lucide-react';
import { PageHeader } from '@/components/layout/PageHeader';
import { PageFooter } from '@/components/layout/PageFooter';
import { Card } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { KpiCard } from '@/components/shared/KpiCard';
import { QueryStateWrapper } from '@/components/shared/QueryStateWrapper';
import { SignInForm, errorText, useSessionToken } from '@/components/auth/SignInForm';
import { DeterministicSections, ExplanationPanel, NarrativeBlock } from '@/components/explanations/ExplanationPanel';
import { useAnalytics, useAnalyticsNarrative, useDailySummary } from '@/hooks/useJournalQueries';
import { ApiError } from '@/lib/api';

// Daily trading summary and journal analytics from the backend (Phase 6). Every figure is the backend's: realized P&L
// comes from the daily_pnl service, counts from the stored records. Money values are exact decimal strings shown as
// received. Analytics are descriptive only: they never change strategies, risk settings or the agent.

type Facts = Record<string, any>; // the backend's facts payload (schemas_journal.py)

function todayIst(offsetDays = 0): string {
  const d = new Date(Date.now() + offsetDays * 86_400_000);
  return d.toLocaleDateString('en-CA', { timeZone: 'Asia/Kolkata' });
}

function CountTable({ title, rows, columns }: { title: string; rows: [string, Facts][]; columns: [string, string][] }) {
  if (rows.length === 0) return null;
  return (
    <div className="overflow-x-auto">
      <h3 className="mb-1 text-[13px] font-semibold text-[#18181B]">{title}</h3>
      <table className="w-full text-left text-[12px]">
        <thead className="text-[#71717A]">
          <tr>
            <th className="py-1 pr-3 font-medium" />
            {columns.map(([, label]) => (
              <th key={label} className="py-1 pr-3 font-medium">
                {label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="text-[#18181B]">
          {rows.map(([name, row]) => (
            <tr key={name} className="border-t border-[#F4F4F5]">
              <td className="py-1 pr-3 font-medium">{name}</td>
              {columns.map(([key]) => (
                <td key={key} className="py-1 pr-3">
                  {row[key] ?? '—'}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function DailySummary({ token, date }: { token: string; date: string }) {
  const summary = useDailySummary(token, date);
  const f = summary.data?.verified_facts as Facts | undefined;
  return (
    <QueryStateWrapper
      isLoading={summary.isLoading}
      isError={summary.isError}
      errorMessage={summary.error ? errorText(summary.error) : undefined}
      onRetry={() => summary.refetch()}
    >
      {f && summary.data && (
        <div className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
            <KpiCard label="Strategy signals" value={String(f.signals.total)}
                     subtext={`${f.signals.entry} entry · ${f.signals.exit} exit`} />
            <KpiCard label="Orders" value={String(f.orders.total)}
                     subtext={`${f.orders.rejected} rejected · ${f.orders.cancelled} cancelled`} />
            <KpiCard label="Closed trades" value={String(f.closed_trades.total)}
                     subtext={`${f.closed_trades.winning} winning · ${f.closed_trades.losing} losing`} />
            <KpiCard label="Net realized P&L (closed)" value={String(f.realized.net_realized_pnl)}
                     subtext={`gross ${f.realized.realized_pnl_gross} · fees ${f.realized.fees}`}
                     tone={Number(f.realized.net_realized_pnl) < 0 ? 'negative' : Number(f.realized.net_realized_pnl) > 0 ? 'positive' : 'neutral'} />
            <KpiCard label="Risk rejections" value={String(f.risk.rejections)}
                     subtext={`${f.risk.evaluations} risk evaluations`} />
          </div>
          <p className="text-[11.5px] text-[#71717A]">
            {f.orders.note} Unrealized P&amp;L is not part of a session summary ({f.unrealized.open_positions_now} positions
            open now; see the portfolio). {f.journal_reflections} journal reflections for this session.
          </p>
          <div className="grid gap-4 lg:grid-cols-2">
            <CountTable title="Strategy activity" rows={Object.entries(f.by_strategy)}
                        columns={[['signals', 'Signals'], ['orders', 'Orders'], ['closed_trades', 'Closed trades']]} />
            <CountTable title="Instrument activity" rows={Object.entries(f.by_instrument).map(([, v]: [string, any]) => [v.symbol ?? '—', v])}
                        columns={[['signals', 'Signals'], ['orders', 'Orders'], ['fills', 'Fills'],
                                  ['risk_rejections', 'Risk rej.'], ['realized_pnl', 'Realized'], ['fees', 'Fees']]} />
          </div>
          {f.agent_events.length > 0 && (
            <div>
              <h3 className="mb-1 text-[13px] font-semibold text-[#18181B]">Agent events</h3>
              <ul className="space-y-0.5 text-[12px] text-[#3F3F46]">
                {f.agent_events.map((e: Facts) => (
                  <li key={e.id}>
                    {e.at_ist} · {e.event_type}
                    {e.instrument ? ` · ${e.instrument}` : ''}
                    {e.to_status ? ` → ${e.to_status}` : ''}
                    {e.reason ? ` (${e.reason})` : ''}
                  </li>
                ))}
              </ul>
            </div>
          )}
          <ExplanationPanel token={token} explanation={summary.data} subject="DAILY_SUMMARY" subjectId={date} />
        </div>
      )}
    </QueryStateWrapper>
  );
}

function Analytics({ token }: { token: string }) {
  const [from, setFrom] = useState(todayIst(-29));
  const [to, setTo] = useState(todayIst());
  const analytics = useAnalytics(token, from, to);
  const narrative = useAnalyticsNarrative(token);
  const a = analytics.data?.analytics as Facts | undefined;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <div className="space-y-1">
          <Label htmlFor="afrom">From</Label>
          <Input id="afrom" type="date" value={from} onChange={(e) => setFrom(e.target.value)} />
        </div>
        <div className="space-y-1">
          <Label htmlFor="ato">To</Label>
          <Input id="ato" type="date" value={to} onChange={(e) => setTo(e.target.value)} />
        </div>
        <Button variant="outline" disabled={narrative.isPending || !a}
                onClick={() => narrative.mutate({ date_from: from, date_to: to })}>
          <Sparkles className="h-3.5 w-3.5" />
          {narrative.isPending ? 'Generating…' : 'AI narrative of these analytics'}
        </Button>
      </div>
      <QueryStateWrapper
        isLoading={analytics.isLoading}
        isError={analytics.isError}
        errorMessage={analytics.error ? errorText(analytics.error) : undefined}
        onRetry={() => analytics.refetch()}
      >
        {a && analytics.data && (
          <div className="space-y-4">
            <div className="rounded-md border border-amber-200 bg-[#FFFBEB] px-3 py-2">
              <p className="flex items-center gap-1.5 text-[12px] font-semibold text-[#B45309]">
                <AlertTriangle className="h-3.5 w-3.5" /> Read with care
              </p>
              <ul className="mt-1 list-disc pl-5 text-[12px] text-[#92400E]">
                {a.caveats.map((c: string) => (
                  <li key={c}>{c}</li>
                ))}
              </ul>
            </div>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <KpiCard label="Closed trades (sample size)" value={String(a.closed_trades.total)} />
              <KpiCard label="Winning / losing" value={`${a.closed_trades.winning} / ${a.closed_trades.losing}`}
                       subtext={a.closed_trades.win_share_pct ? `${a.closed_trades.win_share_pct}% winning` : 'no closed trades'} />
              <KpiCard label="Signals" value={String(a.signals_vs_execution.signals)}
                       subtext={`${a.signals_vs_execution.orders} orders · ${a.signals_vs_execution.orders_filled} filled`} />
              <KpiCard label="Reflections" value={String(a.journal.reflections)} subtext={`${a.journal.entries} journal entries`} />
            </div>
            <div className="grid gap-4 lg:grid-cols-2">
              <CountTable title="By strategy" rows={Object.entries(a.by_strategy)}
                          columns={[['n', 'Trades'], ['winning', 'Win'], ['losing', 'Loss'], ['win_share_pct', 'Win %'],
                                    ['net_realized_pnl_sum', 'Net realized']]} />
              <CountTable title="By strategy and regime (at selection)" rows={Object.entries(a.by_strategy_and_regime)}
                          columns={[['n', 'Trades'], ['winning', 'Win'], ['losing', 'Loss']]} />
              <CountTable title="By instrument" rows={Object.entries(a.by_instrument)}
                          columns={[['n', 'Trades'], ['winning', 'Win'], ['losing', 'Loss']]} />
              <CountTable title="Suppressed signals and risk rejections"
                          rows={Array.from(new Set([...Object.keys(a.suppressed_signals_by_instrument),
                                                    ...Object.keys(a.risk_rejections_by_instrument)])).map((k) => [k, {
                            suppressed: a.suppressed_signals_by_instrument[k] ?? 0,
                            rejections: a.risk_rejections_by_instrument[k] ?? 0,
                          }])}
                          columns={[['suppressed', 'Suppressed signals'], ['rejections', 'Risk rejections']]} />
            </div>
            {Object.keys(a.exit_sources).length > 0 && (
              <p className="text-[12px] text-[#3F3F46]">
                Exits by source:{' '}
                {Object.entries(a.exit_sources).map(([k, v]) => `${k} ${v}`).join(' · ')}
              </p>
            )}
            {Object.keys(a.journal.frequent_words_in_mistakes).length > 0 && (
              <p className="text-[12px] text-[#3F3F46]">
                Frequent words in your recorded mistakes:{' '}
                {Object.entries(a.journal.frequent_words_in_mistakes).map(([k, v]) => `${k} (${v})`).join(', ')}.{' '}
                <span className="text-[#71717A]">{a.journal.note}</span>
              </p>
            )}
            <DeterministicSections explanation={analytics.data} />
            {narrative.isError && <p className="text-[12px] text-[#DC2626]">{errorText(narrative.error)}</p>}
            {narrative.data?.narrative && <NarrativeBlock narrative={narrative.data.narrative} />}
          </div>
        )}
      </QueryStateWrapper>
    </div>
  );
}

export function InsightsPage() {
  const [token, setToken] = useSessionToken();
  const [params, setParams] = useSearchParams();
  const date = params.get('date') ?? todayIst();
  const probe = useDailySummary(token, date);
  const unauthorized = probe.error instanceof ApiError && probe.error.status === 401;

  return (
    <div className="space-y-4">
      <PageHeader
        title="Daily Insights"
        subtitle="What happened, from your stored trading records. Explanations never place or change orders."
        actions={
          token && !unauthorized ? (
            <Button variant="ghost" size="sm" onClick={() => setToken(null)}>
              <LogOut className="h-3.5 w-3.5" />
              Sign out
            </Button>
          ) : undefined
        }
      />
      {(!token || unauthorized) && (
        <Card className="p-5">
          <h2 className="text-[15px] font-semibold text-[#18181B]">Sign in to see your insights</h2>
          <SignInForm onToken={setToken} idPrefix="insights" />
        </Card>
      )}
      {token && !unauthorized && (
        <>
          <Card className="space-y-4 p-5">
            <div className="flex flex-wrap items-end justify-between gap-3">
              <div>
                <h2 className="text-[15px] font-semibold text-[#18181B]">Session summary</h2>
                <p className="text-[11.5px] text-[#71717A]">Exchange date (IST). Paper trading only.</p>
              </div>
              <div className="space-y-1">
                <Label htmlFor="sdate">Session date</Label>
                <Input id="sdate" type="date" value={date} onChange={(e) => setParams({ date: e.target.value })} />
              </div>
            </div>
            <DailySummary token={token} date={date} />
          </Card>
          <Card className="space-y-3 p-5">
            <div>
              <h2 className="text-[15px] font-semibold text-[#18181B]">Journal analytics</h2>
              <p className="text-[11.5px] text-[#71717A]">
                Deterministic counts with sample sizes. Correlation, not causation; no recommendation to change risk.
              </p>
            </div>
            <Analytics token={token} />
          </Card>
        </>
      )}
      <PageFooter />
    </div>
  );
}
