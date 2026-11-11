import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { apiRequest } from '@/lib/api';
import type { AgentAction, AgentDecision, AgentStatusResponse } from '@/types/agent';

// The agent's persisted state, re-read from the backend every 15 s. A transition is shown only once the backend
// reports it: the buttons trigger requests, the panel renders whatever GET /agent/status returns.

export function useAgentStatusQuery(token: string | null) {
  return useQuery({
    queryKey: ['agent', 'status', token],
    queryFn: () => apiRequest<AgentStatusResponse>('/agent/status', { token }),
    enabled: Boolean(token),
    refetchInterval: 15_000,
    retry: false,
  });
}

export function useAgentDecisionsQuery(token: string | null, enabled: boolean) {
  return useQuery({
    queryKey: ['agent', 'decisions', token],
    queryFn: () => apiRequest<AgentDecision[]>('/agent/decisions?limit=10', { token }),
    enabled: Boolean(token) && enabled,
    refetchInterval: 15_000,
    retry: false,
  });
}

export function useAgentActionMutation(token: string | null) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (action: AgentAction) =>
      apiRequest<AgentStatusResponse>(`/agent/${action}`, { method: 'POST', token }),
    onSettled: () => client.invalidateQueries({ queryKey: ['agent'] }),
  });
}

export function useAgentKillSwitchMutation(token: string | null) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (reason: string) =>
      apiRequest<AgentStatusResponse>('/agent/kill-switch', { method: 'POST', token, body: { reason } }),
    onSettled: () => client.invalidateQueries({ queryKey: ['agent'] }),
  });
}
