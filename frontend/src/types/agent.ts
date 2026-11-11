// Shapes of the Phase 5 agent API (backend/app/api/v1/schemas_agent.py). Every value shown in the agent panel comes
// from these responses; nothing here is mock data.

export type AgentStatus = 'STOPPED' | 'RUNNING' | 'PAUSED' | 'ERROR';
export type AgentMode = 'EXECUTION' | 'SIGNALS_ONLY';
export type AgentAction = 'start' | 'pause' | 'resume' | 'stop';

export interface AgentConfig {
  version: number;
  selection_mode: string;
  instrument_ids: number[];
  candidates: { config_id: string; strategy_id: string; priority: number | null }[];
  warmup_sessions: number;
  effective_from_session: string;
}

export interface AgentInstrumentState {
  instrument_id: number;
  status: 'OK' | 'MANUAL_INTERVENTION' | 'ORPHANED_OVERNIGHT' | 'DATA_ERROR';
  status_detail: string | null;
  last_bar_start: string | null;
  owed_exit_since: string | null;
  owed_exit_reason: string | null;
}

export interface AgentStatusResponse {
  configured: boolean;
  status: AgentStatus | null;
  mode: AgentMode | null;
  status_reason: string | null;
  status_changed_at: string | null;
  entries_allowed: boolean;
  blockers: string[];
  disclosures: string[];
  kill_switch_engaged: boolean;
  execution_enabled: boolean;
  execution_blocking_reasons: string[];
  effective_config: AgentConfig | null;
  pending_config: AgentConfig | null;
  instruments: AgentInstrumentState[];
  last_heartbeat_at: string | null;
  worker_id: string | null;
  last_cycle_id: number | null;
  consecutive_failures: number;
  unresolved_protective_exits: number;
}

export interface AgentDecision {
  id: number;
  instrument_id: number;
  kind: 'SELECTION' | 'EVALUATION' | 'OWED_EXIT';
  outcome: string;
  bar_start: string | null;
  config_id: string | null;
  strategy_id: string | null;
  action: string | null;
  reason: string | null;
  detail: string | null;
  client_order_id: string | null;
  order_status: string | null;
  decided_at: string;
}
