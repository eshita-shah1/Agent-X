import { Bot, Database, ListChecks, Sparkles } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { errorText } from '@/components/auth/SignInForm';
import { useGenerateExplanation, useNarratorStatus } from '@/hooks/useJournalQueries';
import type { Explanation, ExplanationSubject, Narrative } from '@/types/journal';

// Three tiers, never mixed: verified facts (copied from stored records), the deterministic rule-based explanation,
// and an optional AI interpretation that is labelled as such and is never an instruction. All text is rendered as
// plain text (React escapes it); nothing generated is ever injected as HTML.

const LAYER_ORDER = [
  'Selection',
  'Strategy signal',
  'Market data',
  'Agent decision',
  'Risk decision',
  'Protective exit',
  'Order',
  'Execution',
  'Outcome',
  'Position now',
];

const STATUS_TEXT: Record<string, string> = {
  DISABLED: 'No LLM provider is configured; the rule-based explanation is shown.',
  FAILED: 'The LLM provider failed or timed out; the rule-based explanation is shown.',
  INVALID_OUTPUT: 'The LLM answer failed validation (e.g. a number not in the facts) and was discarded.',
  RATE_LIMITED: 'The hourly LLM budget is used up; the rule-based explanation is shown.',
  NOT_REQUESTED: 'No AI narrative was requested.',
};

function value(v: unknown): string {
  if (v === null || v === undefined) return '—';
  if (Array.isArray(v)) return v.length ? v.map((x) => String(x)).join(', ') : '—';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

export function NarrativeBlock({ narrative }: { narrative: Narrative }) {
  if (narrative.status !== 'GENERATED' || !narrative.text) {
    return (
      <p className="rounded-md border border-[#E4E4E7] bg-[#F9FAFB] px-3 py-2 text-[12px] text-[#52525B]">
        <span className="font-medium">AI interpretation: {narrative.status}.</span>{' '}
        {STATUS_TEXT[narrative.status] ?? ''}
        {narrative.error ? ` (${narrative.error})` : ''}
      </p>
    );
  }
  return (
    <div className="rounded-md border border-violet-200 bg-violet-50/60 px-3 py-2.5">
      <div className="flex flex-wrap items-center gap-2 text-[11.5px] text-violet-800">
        <Sparkles className="h-3.5 w-3.5" />
        <span className="font-semibold">AI interpretation</span>
        <span className="text-violet-700/80">
          {narrative.provider}:{narrative.model}
          {narrative.latency_ms !== null ? ` · ${narrative.latency_ms} ms` : ''}
        </span>
      </div>
      <p className="mt-1.5 whitespace-pre-wrap text-[12.5px] leading-relaxed text-[#18181B]">{narrative.text}</p>
      {narrative.caveats.length > 0 && (
        <ul className="mt-1.5 list-disc pl-5 text-[11.5px] text-[#52525B]">
          {narrative.caveats.map((c) => (
            <li key={c}>{c}</li>
          ))}
        </ul>
      )}
      <p className="mt-1.5 text-[11px] italic text-[#71717A]">{narrative.label}</p>
    </div>
  );
}

export function DeterministicSections({ explanation }: { explanation: Pick<Explanation, 'deterministic'> }) {
  // Known layers in pipeline order; others (daily summary, analytics) keep the backend's order (stable sort).
  const rank = (layer: string) => {
    const i = LAYER_ORDER.indexOf(layer);
    return i < 0 ? LAYER_ORDER.length : i;
  };
  const sections = [...explanation.deterministic.sections].sort((a, b) => rank(a.layer) - rank(b.layer));
  return (
    <dl className="space-y-1.5">
      {sections.map((s, i) => (
        <div key={`${s.layer}-${i}`} className="grid gap-0.5 sm:grid-cols-[130px_1fr] sm:gap-3">
          <dt className="text-[11.5px] font-semibold uppercase tracking-wide text-[#52647A]">{s.layer}</dt>
          <dd className="text-[12.5px] leading-relaxed text-[#18181B]">{s.text}</dd>
        </div>
      ))}
    </dl>
  );
}

export function ExplanationPanel({
  token,
  explanation,
  subject,
  subjectId,
}: {
  token: string;
  explanation: Explanation;
  subject: ExplanationSubject;
  subjectId: string;
}) {
  const status = useNarratorStatus(token);
  const generate = useGenerateExplanation(token);
  const shown = generate.data ?? explanation;
  const evidence = Object.entries(shown.evidence).filter(([, v]) => v !== null && v !== undefined);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-[11px]">
        <Badge variant="slate" className="gap-1">
          <Database className="h-3 w-3" /> Verified facts
        </Badge>
        <Badge variant="default" className="gap-1">
          <ListChecks className="h-3 w-3" /> Rule-based explanation ({shown.deterministic.generator_version})
        </Badge>
        <Badge variant="outline" className="gap-1 border-violet-200 text-violet-700">
          <Bot className="h-3 w-3" /> AI interpretation (optional)
        </Badge>
      </div>

      <DeterministicSections explanation={shown} />

      {shown.narrative && <NarrativeBlock narrative={shown.narrative} />}

      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          variant="outline"
          disabled={generate.isPending}
          onClick={() => generate.mutate({ subject_type: subject, subject_id: subjectId, narrative: true })}
        >
          <Sparkles className="h-3.5 w-3.5" />
          {generate.isPending ? 'Generating…' : status.data?.enabled ? 'Generate AI narrative' : 'Save explanation'}
        </Button>
        <span className="text-[11.5px] text-[#71717A]">
          {status.data
            ? status.data.enabled
              ? `Provider ${status.data.provider} · ${status.data.model} · ${status.data.llm_calls_last_hour}/${status.data.llm_hourly_budget} calls this hour`
              : `LLM disabled (${status.data.configuration_problems[0] ?? 'LLM_PROVIDER=disabled'}): rule-based only`
            : 'Checking provider…'}
        </span>
      </div>
      {generate.isError && <p className="text-[12px] text-[#DC2626]">{errorText(generate.error)}</p>}

      <details className="rounded-md border border-[#F4F4F5] px-3 py-2">
        <summary className="cursor-pointer text-[12px] font-medium text-[#52525B]">
          Evidence references ({evidence.length})
        </summary>
        <dl className="mt-2 grid gap-x-4 gap-y-1 text-[11.5px] sm:grid-cols-2">
          {evidence.map(([k, v]) => (
            <div key={k} className="flex gap-2 break-all">
              <dt className="shrink-0 text-[#71717A]">{k}</dt>
              <dd className="text-[#18181B]">{value(v)}</dd>
            </div>
          ))}
        </dl>
      </details>
      <details className="rounded-md border border-[#F4F4F5] px-3 py-2">
        <summary className="cursor-pointer text-[12px] font-medium text-[#52525B]">Verified facts (raw)</summary>
        <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-all text-[11px] text-[#3F3F46]">
          {JSON.stringify(shown.verified_facts, null, 2)}
        </pre>
      </details>
    </div>
  );
}
