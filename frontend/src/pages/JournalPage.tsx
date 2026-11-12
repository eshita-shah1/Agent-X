import { useEffect, useRef, useState } from 'react';
import { ChevronLeft, ChevronRight, LogOut, Plus, RefreshCw, Search } from 'lucide-react';
import { PageHeader } from '@/components/layout/PageHeader';
import { PageFooter } from '@/components/layout/PageFooter';
import { Card } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { KpiCard } from '@/components/shared/KpiCard';
import { QueryStateWrapper } from '@/components/shared/QueryStateWrapper';
import { SignInForm, errorText, useSessionToken } from '@/components/auth/SignInForm';
import { EntryDetail, OUTCOME_VARIANT, ist } from '@/components/journal/EntryDetail';
import { EntryForm, selectClass } from '@/components/journal/EntryForm';
import {
  PAGE_SIZE,
  useInstruments,
  useJournalEntry,
  useJournalOverview,
  useJournalSearch,
  useJournalTags,
  useStrategies,
  useSyncJournal,
} from '@/hooks/useJournalQueries';
import { ApiError } from '@/lib/api';
import { cn } from '@/lib/utils';
import type { JournalFilters } from '@/types/journal';

// The trading journal on real backend data (Phase 6). Automatic entries are created from the account's own orders,
// fills, agent decisions and events (POST /journal/sync, idempotent); manual entries are the user's notes. Nothing on
// this page is mock data, and nothing here can place, change or cancel an order.

const EMPTY: JournalFilters = {
  q: '',
  instrument_id: '',
  strategy_id: '',
  date_from: '',
  date_to: '',
  outcome: '',
  entry_type: '',
  tag: '',
};
const OUTCOMES = ['WIN', 'LOSS', 'BREAKEVEN', 'OPEN', 'REJECTED', 'CANCELLED', 'UNKNOWN', 'NONE'];

export function JournalPage() {
  const [token, setToken] = useSessionToken();
  const [draft, setDraft] = useState<JournalFilters>(EMPTY);
  const [filters, setFilters] = useState<JournalFilters>(EMPTY);
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<number | null>(null);
  const [creating, setCreating] = useState(false);

  const overview = useJournalOverview(token);
  const search = useJournalSearch(token, filters, offset);
  const entry = useJournalEntry(token, selected);
  const tags = useJournalTags(token);
  const instruments = useInstruments(token);
  const strategies = useStrategies(token);
  const sync = useSyncJournal(token);
  const synced = useRef(false);

  const unauthorized = [overview.error, search.error].some((e) => e instanceof ApiError && e.status === 401);
  useEffect(() => {
    // Bring automatic entries up to date once per visit (idempotent: repeats create nothing).
    if (token && !unauthorized && !synced.current) {
      synced.current = true;
      sync.mutate(undefined);
    }
  }, [token, unauthorized, sync]);

  const apply = (event?: React.FormEvent) => {
    event?.preventDefault();
    setFilters(draft);
    setOffset(0);
  };
  const setField = (key: keyof JournalFilters) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setDraft((f) => ({ ...f, [key]: e.target.value }));
  const strategyIds = strategies.data?.strategies.map((s) => s.strategy_id) ?? [];
  const page = search.data;

  return (
    <div className="space-y-4">
      <PageHeader
        title="Trade Journal"
        subtitle="Your trading records, notes and reflections. Paper trading only."
        actions={
          token && !unauthorized ? (
            <>
              <Button variant="outline" onClick={() => sync.mutate(undefined)} disabled={sync.isPending}>
                <RefreshCw className={cn('h-3.5 w-3.5', sync.isPending && 'animate-spin')} />
                Sync from trading records
              </Button>
              <Button onClick={() => { setCreating(true); setSelected(null); }}>
                <Plus className="h-3.5 w-3.5" />
                New note
              </Button>
              <Button variant="ghost" size="sm" onClick={() => setToken(null)}>
                <LogOut className="h-3.5 w-3.5" />
                Sign out
              </Button>
            </>
          ) : undefined
        }
      />

      {(!token || unauthorized) && (
        <Card className="p-5">
          <h2 className="text-[15px] font-semibold text-[#18181B]">Sign in to your journal</h2>
          <p className="mt-0.5 text-[11.5px] text-[#71717A]">Journal entries are private to your account.</p>
          <SignInForm onToken={setToken} idPrefix="journal" />
        </Card>
      )}

      {token && !unauthorized && (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <KpiCard label="Journal entries" value={overview.data ? String(overview.data.total) : '—'}
                     subtext={overview.data ? `latest ${ist(overview.data.latest_occurred_at)}` : undefined} />
            <KpiCard label="Automatic entries" value={overview.data ? String(overview.data.automatic) : '—'}
                     subtext="from orders, fills, signals and agent events" />
            <KpiCard label="Manual notes" value={overview.data ? String(overview.data.manual) : '—'} />
            <KpiCard label="Trade reflections" value={overview.data ? String(overview.data.reflections) : '—'} />
          </div>
          {sync.data && (
            <p className="text-[12px] text-[#71717A]">
              Synced the last 30 days: {sync.data.created} new automatic {sync.data.created === 1 ? 'entry' : 'entries'}.
            </p>
          )}
          {sync.isError && <p className="text-[12px] text-[#DC2626]">Sync failed: {errorText(sync.error)}</p>}

          <Card className="p-4">
            <form onSubmit={apply} className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <div className="space-y-1 sm:col-span-2">
                <Label htmlFor="jq">Search titles and notes</Label>
                <div className="relative">
                  <Search className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-[#A1A1AA]" />
                  <Input id="jq" className="pl-8" value={draft.q} onChange={setField('q')} maxLength={200}
                         placeholder="e.g. breakout patience" />
                </div>
              </div>
              <div className="space-y-1">
                <Label htmlFor="jfrom">From</Label>
                <Input id="jfrom" type="date" value={draft.date_from} onChange={setField('date_from')} />
              </div>
              <div className="space-y-1">
                <Label htmlFor="jto">To</Label>
                <Input id="jto" type="date" value={draft.date_to} onChange={setField('date_to')} />
              </div>
              <div className="space-y-1">
                <Label htmlFor="jinst">Instrument</Label>
                <select id="jinst" className={selectClass} value={draft.instrument_id} onChange={setField('instrument_id')}>
                  <option value="">All</option>
                  {(instruments.data ?? []).map((i) => (
                    <option key={i.id} value={i.id}>
                      {i.symbol}
                    </option>
                  ))}
                </select>
              </div>
              <div className="space-y-1">
                <Label htmlFor="jstrat">Strategy</Label>
                <select id="jstrat" className={selectClass} value={draft.strategy_id} onChange={setField('strategy_id')}>
                  <option value="">All</option>
                  {strategyIds.map((s) => (
                    <option key={s} value={s}>
                      {s}
                    </option>
                  ))}
                </select>
              </div>
              <div className="space-y-1">
                <Label htmlFor="jout">Outcome</Label>
                <select id="jout" className={selectClass} value={draft.outcome} onChange={setField('outcome')}>
                  <option value="">All</option>
                  {OUTCOMES.map((o) => (
                    <option key={o} value={o}>
                      {o}
                    </option>
                  ))}
                </select>
              </div>
              <div className="grid grid-cols-2 gap-2">
                <div className="space-y-1">
                  <Label htmlFor="jtype">Type</Label>
                  <select id="jtype" className={selectClass} value={draft.entry_type} onChange={setField('entry_type')}>
                    <option value="">All</option>
                    <option value="AUTO">Automatic</option>
                    <option value="MANUAL">Manual</option>
                  </select>
                </div>
                <div className="space-y-1">
                  <Label htmlFor="jtag">Tag</Label>
                  <select id="jtag" className={selectClass} value={draft.tag} onChange={setField('tag')}>
                    <option value="">All</option>
                    {(tags.data ?? []).map((t) => (
                      <option key={t.name} value={t.name}>
                        {t.name} ({t.entries})
                      </option>
                    ))}
                  </select>
                </div>
              </div>
              <div className="flex items-end gap-2 sm:col-span-2 lg:col-span-4 lg:justify-end">
                <Button type="button" variant="ghost" onClick={() => { setDraft(EMPTY); setFilters(EMPTY); setOffset(0); }}>
                  Clear
                </Button>
                <Button type="submit">Apply filters</Button>
              </div>
            </form>
          </Card>

          <div className="grid gap-4 xl:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
            <div className="space-y-2">
              <QueryStateWrapper
                isLoading={search.isLoading}
                isError={search.isError}
                errorMessage={search.error ? errorText(search.error) : undefined}
                onRetry={() => search.refetch()}
                isEmpty={page?.items.length === 0}
                emptyTitle="No journal entries"
                emptyDescription="Sync from your trading records or write a note. Filters may be hiding entries."
              >
                <Card className="divide-y divide-[#F4F4F5]">
                  {page?.items.map((e) => (
                    <button
                      key={e.id}
                      type="button"
                      onClick={() => { setSelected(e.id); setCreating(false); }}
                      className={cn(
                        'block w-full px-4 py-3 text-left transition-colors hover:bg-[#F9FAFB]',
                        selected === e.id && 'bg-[#F4F4F5]'
                      )}
                    >
                      <div className="flex items-start justify-between gap-2">
                        <p className="text-[13px] font-medium text-[#18181B]">{e.title}</p>
                        {e.outcome !== 'NONE' && <Badge variant={OUTCOME_VARIANT[e.outcome]}>{e.outcome}</Badge>}
                      </div>
                      <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[11px] text-[#71717A]">
                        <Badge variant={e.entry_type === 'AUTO' ? 'slate' : 'outline'}>
                          {e.entry_type === 'AUTO' ? 'Auto' : 'Manual'}
                        </Badge>
                        <span>{ist(e.occurred_at)}</span>
                        {e.has_reflection && <span>· reflected</span>}
                        {e.tags.map((t) => (
                          <span key={t}>#{t}</span>
                        ))}
                      </div>
                    </button>
                  ))}
                </Card>
              </QueryStateWrapper>
              {page && page.total > 0 && (
                <div className="flex items-center justify-between text-[12px] text-[#71717A]">
                  <span>
                    {offset + 1}–{Math.min(offset + PAGE_SIZE, page.total)} of {page.total}
                  </span>
                  <div className="flex gap-1">
                    <Button size="icon" variant="outline" aria-label="Previous page" disabled={offset === 0}
                            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>
                      <ChevronLeft className="h-4 w-4" />
                    </Button>
                    <Button size="icon" variant="outline" aria-label="Next page" disabled={offset + PAGE_SIZE >= page.total}
                            onClick={() => setOffset(offset + PAGE_SIZE)}>
                      <ChevronRight className="h-4 w-4" />
                    </Button>
                  </div>
                </div>
              )}
            </div>

            <Card className="p-5">
              {creating && (
                <>
                  <h2 className="mb-3 text-[15px] font-semibold text-[#18181B]">New journal note</h2>
                  <EntryForm token={token} entry={null} instruments={instruments.data ?? []} strategies={strategyIds}
                             onDone={(id) => { setCreating(false); setSelected(id); }} />
                </>
              )}
              {!creating && selected === null && (
                <p className="text-[12.5px] text-[#71717A]">
                  Select an entry to see its trading facts, explanation and your notes, or create a new note.
                </p>
              )}
              {!creating && selected !== null && entry.isLoading && (
                <p className="text-[12px] text-[#71717A]">Loading entry…</p>
              )}
              {!creating && selected !== null && entry.isError && (
                <p className="text-[12px] text-[#DC2626]">{errorText(entry.error)}</p>
              )}
              {!creating && entry.data && selected === entry.data.id && (
                <EntryDetail key={entry.data.id} token={token} entry={entry.data} instruments={instruments.data ?? []}
                             strategies={strategyIds} onDeleted={() => setSelected(null)} />
              )}
            </Card>
          </div>
        </>
      )}

      <PageFooter />
    </div>
  );
}
