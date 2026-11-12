import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { apiRequest } from '@/lib/api';
import type {
  Analytics,
  Explanation,
  ExplanationSubject,
  InstrumentOption,
  JournalEntryDetail,
  JournalFilters,
  JournalOverview,
  JournalPage,
  NarratorStatus,
  Reflection,
  ReflectionAssist,
} from '@/types/journal';

// Phase 6 journal and explanation endpoints. Reads only what the backend returns; writes only journal notes,
// reflections and stored explanations (no route here can touch orders, risk settings or the agent).

export const PAGE_SIZE = 20;

function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') search.append(key, String(value));
  }
  const s = search.toString();
  return s ? `?${s}` : '';
}

export function useJournalSearch(token: string | null, filters: JournalFilters, offset: number) {
  return useQuery({
    queryKey: ['journal', 'search', token, filters, offset],
    queryFn: () =>
      apiRequest<JournalPage>(`/journal${query({ ...filters, limit: PAGE_SIZE, offset })}`, { token }),
    enabled: Boolean(token),
    placeholderData: keepPreviousData,
    retry: false,
  });
}

export function useJournalOverview(token: string | null) {
  return useQuery({
    queryKey: ['journal', 'overview', token],
    queryFn: () => apiRequest<JournalOverview>('/journal/overview', { token }),
    enabled: Boolean(token),
    retry: false,
  });
}

export function useJournalEntry(token: string | null, id: number | null) {
  return useQuery({
    queryKey: ['journal', 'entry', token, id],
    queryFn: () => apiRequest<JournalEntryDetail>(`/journal/${id}`, { token }),
    enabled: Boolean(token) && id !== null,
    retry: false,
  });
}

export function useJournalTags(token: string | null) {
  return useQuery({
    queryKey: ['journal', 'tags', token],
    queryFn: () => apiRequest<{ name: string; entries: number }[]>('/journal/tags', { token }),
    enabled: Boolean(token),
    retry: false,
  });
}

export function useInstruments(token: string | null) {
  return useQuery({
    queryKey: ['market', 'instruments', token],
    queryFn: () => apiRequest<InstrumentOption[]>('/market/instruments', { token }),
    enabled: Boolean(token),
    staleTime: 5 * 60_000,
    retry: false,
  });
}

export function useStrategies(token: string | null) {
  return useQuery({
    queryKey: ['backtests', 'strategies', token],
    queryFn: () => apiRequest<{ strategies: { strategy_id: string }[] }>('/backtests/strategies', { token }),
    enabled: Boolean(token),
    staleTime: 5 * 60_000,
    retry: false,
  });
}

function useJournalMutation<TVars, TResult>(fn: (vars: TVars) => Promise<TResult>) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSettled: () => client.invalidateQueries({ queryKey: ['journal'] }),
  });
}

export function useSyncJournal(token: string | null) {
  return useJournalMutation(() =>
    apiRequest<{ created: number; candidates: number }>('/journal/sync', { method: 'POST', token, body: {} })
  );
}

export function useSaveEntry(token: string | null) {
  return useJournalMutation(({ id, body }: { id: number | null; body: Record<string, unknown> }) =>
    id === null
      ? apiRequest<JournalEntryDetail>('/journal', { method: 'POST', token, body })
      : apiRequest<JournalEntryDetail>(`/journal/${id}`, { method: 'PATCH', token, body })
  );
}

export function useDeleteEntry(token: string | null) {
  return useJournalMutation((id: number) => apiRequest<void>(`/journal/${id}`, { method: 'DELETE', token }));
}

export function useSaveReflection(token: string | null) {
  return useJournalMutation(({ id, body }: { id: number; body: Partial<Reflection> }) =>
    apiRequest<Reflection>(`/journal/${id}/reflection`, { method: 'POST', token, body })
  );
}

export function useReflectionAssist(token: string | null) {
  return useMutation({
    mutationFn: (id: number) =>
      apiRequest<ReflectionAssist>(`/journal/${id}/reflection/assist`, { method: 'POST', token }),
  });
}

// ---- explanations ----------------------------------------------------------------------------------------------

export function useNarratorStatus(token: string | null) {
  return useQuery({
    queryKey: ['explanations', 'provider', token],
    queryFn: () => apiRequest<NarratorStatus>('/explanations/provider', { token }),
    enabled: Boolean(token),
    retry: false,
  });
}

const PREVIEW_PATH: Partial<Record<ExplanationSubject, string>> = {
  AGENT_DECISION: 'decisions',
  ORDER: 'orders',
  AGENT_EVENT: 'events',
};

export function useExplanationPreview(token: string | null, subject: ExplanationSubject | null, id: string | null) {
  return useQuery({
    queryKey: ['explanations', 'preview', token, subject, id],
    queryFn: () => apiRequest<Explanation>(`/explanations/${PREVIEW_PATH[subject!]}/${id}`, { token }),
    enabled: Boolean(token) && subject !== null && id !== null && Boolean(PREVIEW_PATH[subject]),
    retry: false,
  });
}

export function useDailySummary(token: string | null, sessionDate: string) {
  return useQuery({
    queryKey: ['explanations', 'daily', token, sessionDate],
    queryFn: () => apiRequest<Explanation>(`/explanations/daily-summary${query({ session_date: sessionDate })}`, { token }),
    enabled: Boolean(token) && Boolean(sessionDate),
    retry: false,
  });
}

export function useGenerateExplanation(token: string | null) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: { subject_type: ExplanationSubject; subject_id: string; narrative: boolean }) =>
      apiRequest<Explanation>('/explanations/generate', { method: 'POST', token, body }),
    onSettled: () => client.invalidateQueries({ queryKey: ['explanations', 'provider'] }),
  });
}

export function useAnalytics(token: string | null, from: string, to: string) {
  return useQuery({
    queryKey: ['journal', 'analytics', token, from, to],
    queryFn: () => apiRequest<Analytics>(`/journal/analytics${query({ date_from: from, date_to: to })}`, { token }),
    enabled: Boolean(token),
    retry: false,
  });
}

export function useAnalyticsNarrative(token: string | null) {
  return useMutation({
    mutationFn: (body: { date_from: string; date_to: string }) =>
      apiRequest<Explanation>('/journal/analytics/narrative', { method: 'POST', token, body }),
  });
}
