import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { errorText } from '@/components/auth/SignInForm';
import { useSaveEntry } from '@/hooks/useJournalQueries';
import type { InstrumentOption, JournalEntryDetail } from '@/types/journal';

export const NOTE_FIELDS: { key: keyof JournalEntryDetail; label: string }[] = [
  { key: 'entry_thesis', label: 'Entry thesis' },
  { key: 'exit_thesis', label: 'Exit thesis' },
  { key: 'market_observations', label: 'Market observations' },
  { key: 'went_well', label: 'What went well' },
  { key: 'went_wrong', label: 'What went wrong' },
  { key: 'lessons', label: 'Lessons learned' },
  { key: 'emotional_notes', label: 'Emotional discipline notes' },
  { key: 'notes', label: 'Notes' },
];

export const textareaClass =
  'w-full rounded-lg border border-[#E4E4E7] bg-[#F9FAFB] px-3 py-2 text-[13px] text-[#18181B] placeholder:text-[#71717A] focus-visible:border-[#52647A] focus-visible:bg-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#52647A]/15';
export const selectClass =
  'h-9 w-full rounded-lg border border-[#E4E4E7] bg-white px-2.5 text-[13px] text-[#18181B] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#52647A]/20';

/** Create a manual entry (entry = null) or edit an entry. On an automatic entry only notes, rating, confidence and
 *  tags are editable: its title, links and outcome are system facts. */
export function EntryForm({
  token,
  entry,
  instruments,
  strategies,
  onDone,
}: {
  token: string;
  entry: JournalEntryDetail | null;
  instruments: InstrumentOption[];
  strategies: string[];
  onDone: (id: number | null) => void;
}) {
  const save = useSaveEntry(token);
  const isAuto = entry?.entry_type === 'AUTO';
  const [form, setForm] = useState<Record<string, string>>(() => {
    const initial: Record<string, string> = {
      title: entry?.title ?? '',
      instrument_id: entry?.instrument_id ? String(entry.instrument_id) : '',
      strategy_id: entry?.strategy_id ?? '',
      order_id: entry?.order_id ?? '',
      rating: entry?.rating ? String(entry.rating) : '',
      confidence: entry?.confidence ? String(entry.confidence) : '',
      tags: entry?.tags.join(', ') ?? '',
    };
    for (const f of NOTE_FIELDS) initial[f.key] = (entry?.[f.key] as string | null) ?? '';
    return initial;
  });
  const set = (key: string) => (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) =>
    setForm((f) => ({ ...f, [key]: e.target.value }));

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    const body: Record<string, unknown> = {
      rating: form.rating ? Number(form.rating) : null,
      confidence: form.confidence ? Number(form.confidence) : null,
      tags: form.tags
        .split(',')
        .map((t) => t.trim())
        .filter(Boolean),
    };
    for (const f of NOTE_FIELDS) body[f.key] = form[f.key].trim() ? form[f.key] : null;
    if (!isAuto) {
      body.title = form.title.trim();
      body.instrument_id = form.instrument_id ? Number(form.instrument_id) : null;
      body.strategy_id = form.strategy_id || null;
      body.order_id = form.order_id.trim() || null;
    }
    save.mutate({ id: entry?.id ?? null, body }, { onSuccess: (saved) => onDone(saved.id) });
  };

  return (
    <form onSubmit={submit} className="space-y-3">
      {isAuto ? (
        <p className="rounded-md bg-[#F4F4F5] px-3 py-2 text-[12px] text-[#52525B]">
          Automatic entry: its title, links and outcome are system facts and stay unchanged. Your notes below are
          yours to edit.
        </p>
      ) : (
        <>
          <div className="space-y-1">
            <Label htmlFor="entry-title">Title</Label>
            <Input id="entry-title" value={form.title} onChange={set('title')} maxLength={200} required />
          </div>
          <div className="grid gap-3 sm:grid-cols-3">
            <div className="space-y-1">
              <Label htmlFor="entry-instrument">Instrument</Label>
              <select id="entry-instrument" className={selectClass} value={form.instrument_id} onChange={set('instrument_id')}>
                <option value="">None</option>
                {instruments.map((i) => (
                  <option key={i.id} value={i.id}>
                    {i.symbol}
                  </option>
                ))}
              </select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="entry-strategy">Strategy</Label>
              <select id="entry-strategy" className={selectClass} value={form.strategy_id} onChange={set('strategy_id')}>
                <option value="">None</option>
                {strategies.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="entry-order">Linked order id (optional)</Label>
              <Input id="entry-order" value={form.order_id} onChange={set('order_id')} placeholder="order UUID" />
            </div>
          </div>
        </>
      )}
      <div className="grid gap-3 sm:grid-cols-2">
        {NOTE_FIELDS.map((f) => (
          <div key={f.key} className="space-y-1">
            <Label htmlFor={`entry-${f.key}`}>{f.label}</Label>
            <textarea
              id={`entry-${f.key}`}
              rows={2}
              maxLength={10000}
              className={textareaClass}
              value={form[f.key]}
              onChange={set(f.key)}
            />
          </div>
        ))}
      </div>
      <div className="grid gap-3 sm:grid-cols-3">
        <div className="space-y-1">
          <Label htmlFor="entry-tags">Tags (comma separated)</Label>
          <Input id="entry-tags" value={form.tags} onChange={set('tags')} placeholder="breakout, patience" />
        </div>
        <div className="space-y-1">
          <Label htmlFor="entry-rating">Rating (1–5)</Label>
          <select id="entry-rating" className={selectClass} value={form.rating} onChange={set('rating')}>
            <option value="">—</option>
            {[1, 2, 3, 4, 5].map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </div>
        <div className="space-y-1">
          <Label htmlFor="entry-confidence">Confidence (1–5)</Label>
          <select id="entry-confidence" className={selectClass} value={form.confidence} onChange={set('confidence')}>
            <option value="">—</option>
            {[1, 2, 3, 4, 5].map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </div>
      </div>
      {save.isError && <p className="text-[12px] text-[#DC2626]">{errorText(save.error)}</p>}
      <div className="flex justify-end gap-2">
        <Button type="button" variant="outline" onClick={() => onDone(entry?.id ?? null)}>
          Cancel
        </Button>
        <Button type="submit" disabled={save.isPending}>
          {save.isPending ? 'Saving…' : entry ? 'Save notes' : 'Create entry'}
        </Button>
      </div>
    </form>
  );
}
