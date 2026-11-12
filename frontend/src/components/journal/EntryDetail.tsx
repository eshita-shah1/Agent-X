import { useState } from 'react';
import { Link } from 'react-router-dom';
import { Lightbulb, Pencil, Trash2 } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { ConfirmDialog } from '@/components/shared/ConfirmDialog';
import { errorText } from '@/components/auth/SignInForm';
import { ExplanationPanel, NarrativeBlock } from '@/components/explanations/ExplanationPanel';
import { EntryForm, NOTE_FIELDS, selectClass, textareaClass } from '@/components/journal/EntryForm';
import {
  useDeleteEntry,
  useExplanationPreview,
  useReflectionAssist,
  useSaveReflection,
} from '@/hooks/useJournalQueries';
import type { ExplanationSubject, InstrumentOption, JournalEntryDetail, Reflection, TradeOutcome } from '@/types/journal';

export const OUTCOME_VARIANT: Record<TradeOutcome, 'positive' | 'negative' | 'warning' | 'default' | 'slate'> = {
  WIN: 'positive',
  LOSS: 'negative',
  BREAKEVEN: 'slate',
  OPEN: 'warning',
  REJECTED: 'negative',
  CANCELLED: 'default',
  UNKNOWN: 'default',
  NONE: 'default',
};

export function ist(value: string | null | undefined): string {
  return value ? new Date(value).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata' }) + ' IST' : '—';
}

function show(v: unknown): string {
  if (v === null || v === undefined || v === '') return '—';
  return Array.isArray(v) ? v.join(', ') : String(v);
}

function subjectOf(entry: JournalEntryDetail): [ExplanationSubject, string] | [null, null] {
  if (entry.agent_decision_id) return ['AGENT_DECISION', String(entry.agent_decision_id)];
  if (entry.order_id) return ['ORDER', entry.order_id];
  if (entry.agent_event_id) return ['AGENT_EVENT', String(entry.agent_event_id)];
  return [null, null];
}

function LinkedTrade({ entry }: { entry: JournalEntryDetail }) {
  const { trade, fills, order, position } = entry.linked;
  if (!trade && !order && !position && fills.length === 0) return null;
  return (
    <section className="space-y-2">
      <h3 className="text-[13px] font-semibold text-[#18181B]">Linked trade (live from trading records)</h3>
      {trade && (
        <div className="flex flex-wrap gap-x-5 gap-y-1 text-[12px] text-[#3F3F46]">
          <span>Status: {show(trade.status)}</span>
          <span>Outcome: {show(trade.outcome)}</span>
          <span>Realized P&amp;L (gross): {show(trade.realized_pnl)}</span>
          <span>Fees: {show(trade.fees)}</span>
          <span>Net realized: {show(trade.net_realized_pnl)}</span>
          <span>Closed: {ist(trade.closed_at as string | null)}</span>
        </div>
      )}
      {order && (
        <p className="text-[12px] text-[#3F3F46]">
          Order {show(order.client_order_id)}: {show(order.source)} {show(order.side)} {show(order.order_type)},
          quantity {show(order.quantity)}, filled {show(order.filled_quantity)}, status {show(order.status)}
          {order.reject_reason ? ` — ${order.reject_reason}` : ''}
        </p>
      )}
      {fills.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-[11.5px]">
            <thead className="text-[#71717A]">
              <tr>
                <th className="py-1 pr-3 font-medium">Fill</th>
                <th className="py-1 pr-3 font-medium">Side</th>
                <th className="py-1 pr-3 font-medium">Qty</th>
                <th className="py-1 pr-3 font-medium">Price</th>
                <th className="py-1 pr-3 font-medium">Fee</th>
                <th className="py-1 pr-3 font-medium">Realized</th>
                <th className="py-1 font-medium">Simulated at</th>
              </tr>
            </thead>
            <tbody>
              {fills.map((f) => (
                <tr key={String(f.id)} className="border-t border-[#F4F4F5]">
                  <td className="py-1 pr-3">{show(f.id)}</td>
                  <td className="py-1 pr-3">{show(f.side)}</td>
                  <td className="py-1 pr-3">{show(f.quantity)}</td>
                  <td className="py-1 pr-3">{show(f.price)}</td>
                  <td className="py-1 pr-3">{show(f.fee)}</td>
                  <td className="py-1 pr-3">{show(f.realized_pnl)}</td>
                  <td className="py-1 whitespace-nowrap">{ist(f.price_as_of as string)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-1 text-[11px] text-[#71717A]">
            Simulated paper fills ({show(fills[0].execution_policy)}, {show(fills[0].price_basis)}); not verified
            executions.
          </p>
        </div>
      )}
      {position && (
        <p className="text-[12px] text-[#3F3F46]">
          Position now: {show(position.quantity_now)} shares; realized to date {show(position.realized_pnl_to_date)},
          fees to date {show(position.fees_paid_to_date)}.
        </p>
      )}
    </section>
  );
}

type TriState = '' | 'yes' | 'no';
const tri = (v: boolean | null | undefined): TriState => (v === true ? 'yes' : v === false ? 'no' : '');
const fromTri = (v: TriState): boolean | null => (v === 'yes' ? true : v === 'no' ? false : null);

function ReflectionSection({ token, entry }: { token: string; entry: JournalEntryDetail }) {
  const save = useSaveReflection(token);
  const assist = useReflectionAssist(token);
  const r = entry.reflection;
  const [form, setForm] = useState({
    original_thesis: r?.original_thesis ?? '',
    mistakes: r?.mistakes ?? '',
    lessons: r?.lessons ?? '',
    future_improvements: r?.future_improvements ?? '',
    followed_strategy_rules: tri(r?.followed_strategy_rules),
    risk_limits_respected: tri(r?.risk_limits_respected),
    exit_followed_strategy: tri(r?.exit_followed_strategy),
  });
  const set = (key: keyof typeof form) => (e: React.ChangeEvent<HTMLTextAreaElement | HTMLSelectElement>) =>
    setForm((f) => ({ ...f, [key]: e.target.value }));
  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    const body: Partial<Reflection> = {
      original_thesis: form.original_thesis || null,
      mistakes: form.mistakes || null,
      lessons: form.lessons || null,
      future_improvements: form.future_improvements || null,
      followed_strategy_rules: fromTri(form.followed_strategy_rules as TriState),
      risk_limits_respected: fromTri(form.risk_limits_respected as TriState),
      exit_followed_strategy: fromTri(form.exit_followed_strategy as TriState),
    };
    save.mutate({ id: entry.id, body });
  };
  const questions: [keyof typeof form, string][] = [
    ['followed_strategy_rules', 'Did the setup follow the strategy rules?'],
    ['risk_limits_respected', 'Were the risk limits respected?'],
    ['exit_followed_strategy', 'Did the exit follow the strategy?'],
  ];

  return (
    <section className="space-y-2 rounded-lg border border-[#E4E4E7] p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-[13px] font-semibold text-[#18181B]">Trade reflection</h3>
        <Button type="button" size="sm" variant="outline" disabled={assist.isPending} onClick={() => assist.mutate(entry.id)}>
          <Lightbulb className="h-3.5 w-3.5" />
          {assist.isPending ? 'Preparing…' : 'Suggest questions'}
        </Button>
      </div>
      {assist.isError && <p className="text-[12px] text-[#DC2626]">{errorText(assist.error)}</p>}
      {assist.data && (
        <div className="space-y-2 rounded-md bg-[#F9FAFB] p-2.5">
          <p className="text-[11.5px] font-medium text-[#52525B]">Questions to consider (nothing is saved until you save):</p>
          <ul className="list-disc pl-5 text-[12px] text-[#3F3F46]">
            {assist.data.questions.map((q) => (
              <li key={q}>{q}</li>
            ))}
          </ul>
          {assist.data.draft && <NarrativeBlock narrative={assist.data.draft} />}
        </div>
      )}
      <form onSubmit={submit} className="space-y-2">
        <div className="grid gap-2 sm:grid-cols-3">
          {questions.map(([key, label]) => (
            <div key={key} className="space-y-1">
              <Label htmlFor={`refl-${key}`}>{label}</Label>
              <select id={`refl-${key}`} className={selectClass} value={form[key]} onChange={set(key)}>
                <option value="">Not answered</option>
                <option value="yes">Yes</option>
                <option value="no">No</option>
              </select>
            </div>
          ))}
        </div>
        <div className="grid gap-2 sm:grid-cols-2">
          {(
            [
              ['original_thesis', 'Original trading thesis'],
              ['mistakes', 'Mistakes'],
              ['lessons', 'Lessons'],
              ['future_improvements', 'Future improvements'],
            ] as [keyof typeof form, string][]
          ).map(([key, label]) => (
            <div key={key} className="space-y-1">
              <Label htmlFor={`refl-${key}`}>{label}</Label>
              <textarea id={`refl-${key}`} rows={2} maxLength={10000} className={textareaClass} value={form[key]}
                        onChange={set(key)} />
            </div>
          ))}
        </div>
        {save.isError && <p className="text-[12px] text-[#DC2626]">{errorText(save.error)}</p>}
        {save.isSuccess && <p className="text-[12px] text-[#15803D]">Reflection saved.</p>}
        <div className="flex justify-end">
          <Button type="submit" size="sm" disabled={save.isPending}>
            {save.isPending ? 'Saving…' : r ? 'Update reflection' : 'Save reflection'}
          </Button>
        </div>
      </form>
    </section>
  );
}

export function EntryDetail({
  token,
  entry,
  instruments,
  strategies,
  onDeleted,
}: {
  token: string;
  entry: JournalEntryDetail;
  instruments: InstrumentOption[];
  strategies: string[];
  onDeleted: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const remove = useDeleteEntry(token);
  const [subject, subjectId] = subjectOf(entry);
  const preview = useExplanationPreview(token, subject, subjectId);
  const facts = Object.entries(entry.system_facts).filter(([, v]) => v !== null && v !== '');

  if (editing) {
    return (
      <EntryForm token={token} entry={entry} instruments={instruments} strategies={strategies}
                 onDone={() => setEditing(false)} />
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h2 className="text-[15px] font-semibold text-[#18181B]">{entry.title}</h2>
          <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[11.5px] text-[#71717A]">
            <Badge variant={entry.entry_type === 'AUTO' ? 'slate' : 'outline'}>
              {entry.entry_type === 'AUTO' ? 'Automatic' : 'Manual'}
            </Badge>
            {entry.source_event && <Badge variant="default">{entry.source_event}</Badge>}
            {entry.outcome !== 'NONE' && <Badge variant={OUTCOME_VARIANT[entry.outcome]}>{entry.outcome}</Badge>}
            {entry.symbol && <span>{entry.symbol}</span>}
            {entry.strategy_id && <span>· {entry.strategy_id}</span>}
            <span>· {ist(entry.occurred_at)}</span>
          </div>
          {entry.tags.length > 0 && (
            <div className="mt-1.5 flex flex-wrap gap-1">
              {entry.tags.map((t) => (
                <Badge key={t} variant="outline">
                  #{t}
                </Badge>
              ))}
            </div>
          )}
        </div>
        <div className="flex gap-2">
          <Button size="sm" variant="outline" onClick={() => setEditing(true)}>
            <Pencil className="h-3.5 w-3.5" />
            {entry.entry_type === 'AUTO' ? 'Edit notes' : 'Edit'}
          </Button>
          {entry.entry_type === 'MANUAL' && (
            <Button size="sm" variant="dangerOutline" onClick={() => setConfirmDelete(true)}>
              <Trash2 className="h-3.5 w-3.5" />
              Delete
            </Button>
          )}
        </div>
      </div>
      {remove.isError && <p className="text-[12px] text-[#DC2626]">{errorText(remove.error)}</p>}

      {entry.entry_type === 'AUTO' && facts.length > 0 && (
        <section>
          <h3 className="text-[13px] font-semibold text-[#18181B]">System facts (recorded, read-only)</h3>
          <dl className="mt-1 grid gap-x-4 gap-y-0.5 text-[11.5px] sm:grid-cols-2">
            {facts.map(([k, v]) => (
              <div key={k} className="flex gap-2 break-all">
                <dt className="shrink-0 text-[#71717A]">{k}</dt>
                <dd className="text-[#18181B]">{show(typeof v === 'object' && !Array.isArray(v) ? JSON.stringify(v) : v)}</dd>
              </div>
            ))}
          </dl>
        </section>
      )}

      <LinkedTrade entry={entry} />

      {NOTE_FIELDS.some((f) => entry[f.key]) && (
        <section className="space-y-1.5">
          <h3 className="text-[13px] font-semibold text-[#18181B]">Your notes</h3>
          {NOTE_FIELDS.filter((f) => entry[f.key]).map((f) => (
            <div key={f.key}>
              <p className="text-[11.5px] font-medium text-[#71717A]">{f.label}</p>
              <p className="whitespace-pre-wrap text-[12.5px] text-[#18181B]">{entry[f.key] as string}</p>
            </div>
          ))}
          {(entry.rating || entry.confidence) && (
            <p className="text-[12px] text-[#52525B]">
              Rating {entry.rating ?? '—'} / 5 · Confidence {entry.confidence ?? '—'} / 5
            </p>
          )}
        </section>
      )}

      {entry.source_event === 'SESSION_SUMMARY' && (
        <p className="text-[12.5px] text-[#52525B]">
          The session's figures are read live on the{' '}
          <Link className="font-medium text-[#52647A] underline" to={`/insights?date=${entry.session_date}`}>
            Daily Insights
          </Link>{' '}
          page.
        </p>
      )}

      {subject && subjectId && (
        <section className="space-y-2">
          <h3 className="text-[13px] font-semibold text-[#18181B]">Explanation</h3>
          {preview.isLoading && <p className="text-[12px] text-[#71717A]">Loading explanation…</p>}
          {preview.isError && <p className="text-[12px] text-[#DC2626]">{errorText(preview.error)}</p>}
          {preview.data && <ExplanationPanel token={token} explanation={preview.data} subject={subject} subjectId={subjectId} />}
        </section>
      )}

      {entry.reflection_allowed && <ReflectionSection key={entry.id} token={token} entry={entry} />}

      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title="Delete this manual entry?"
        description="Your note, its tags and reflection are removed. Trading records are not affected."
        confirmLabel="Delete"
        destructive
        onConfirm={() => remove.mutate(entry.id, { onSuccess: onDeleted })}
      />
    </div>
  );
}
