// Shapes of the Phase 6 explanation and journal API (backend/app/api/v1/schemas_journal.py). Every value shown on the
// Journal and Insights pages comes from these responses; nothing here is mock data. Money values arrive as exact
// decimal strings and are displayed as received.

export type NarrativeStatus = 'NOT_REQUESTED' | 'DISABLED' | 'GENERATED' | 'FAILED' | 'INVALID_OUTPUT' | 'RATE_LIMITED';
export type ExplanationSubject = 'AGENT_DECISION' | 'ORDER' | 'AGENT_EVENT' | 'DAILY_SUMMARY' | 'JOURNAL_INSIGHTS';
export type TradeOutcome = 'WIN' | 'LOSS' | 'BREAKEVEN' | 'OPEN' | 'REJECTED' | 'CANCELLED' | 'UNKNOWN' | 'NONE';
export type EntryType = 'MANUAL' | 'AUTO';

export interface Section {
  layer: string;
  text: string;
}

export interface Narrative {
  status: NarrativeStatus;
  text: string | null;
  caveats: string[];
  provider: string | null;
  model: string | null;
  error: string | null;
  latency_ms: number | null;
  label: string;
}

export interface Explanation {
  id: number | null;
  persisted: boolean;
  subject_type: ExplanationSubject;
  subject_key: string;
  verified_facts: Record<string, unknown>;
  evidence: Record<string, unknown>;
  facts_fingerprint: string;
  deterministic: { kind: string; generator_version: string; text: string; sections: Section[] };
  narrative: Narrative | null;
  created_at: string | null;
}

export interface NarratorStatus {
  enabled: boolean;
  provider: string;
  model: string | null;
  configuration_problems: string[];
  includes_journal_notes: boolean;
  fallback: string;
  llm_calls_last_hour: number;
  llm_hourly_budget: number;
}

export interface JournalNotes {
  notes: string | null;
  entry_thesis: string | null;
  exit_thesis: string | null;
  market_observations: string | null;
  went_well: string | null;
  went_wrong: string | null;
  lessons: string | null;
  emotional_notes: string | null;
  rating: number | null;
  confidence: number | null;
}

export interface JournalEntry extends JournalNotes {
  id: number;
  entry_type: EntryType;
  source_event: string | null;
  title: string;
  occurred_at: string;
  session_date: string;
  outcome: TradeOutcome;
  instrument_id: number | null;
  symbol: string | null;
  strategy_id: string | null;
  order_id: string | null;
  fill_id: number | null;
  position_id: number | null;
  agent_decision_id: number | null;
  agent_event_id: number | null;
  config_version_id: number | null;
  backtest_run_id: string | null;
  system_facts: Record<string, unknown>;
  tags: string[];
  has_reflection: boolean;
  created_at: string;
  updated_at: string;
}

export interface Reflection {
  original_thesis: string | null;
  followed_strategy_rules: boolean | null;
  risk_limits_respected: boolean | null;
  exit_followed_strategy: boolean | null;
  mistakes: string | null;
  lessons: string | null;
  future_improvements: string | null;
  created_at?: string;
  updated_at?: string;
}

export interface LinkedTrade {
  instrument: { id: number; symbol: string | null } | null;
  order: Record<string, string | number | null> | null;
  fills: Record<string, string | number | null>[];
  position: Record<string, string | number | null> | null;
  trade: Record<string, string | number | string[] | null> | null;
  agent_decision: Record<string, string | number | null> | null;
  source: string;
}

export interface JournalEntryDetail extends JournalEntry {
  reflection: Reflection | null;
  linked: LinkedTrade;
  reflection_allowed: boolean;
}

export interface JournalPage {
  items: JournalEntry[];
  total: number;
  limit: number;
  offset: number;
}

export interface JournalOverview {
  total: number;
  manual: number;
  automatic: number;
  reflections: number;
  latest_occurred_at: string | null;
}

export interface JournalFilters {
  q: string;
  instrument_id: string;
  strategy_id: string;
  date_from: string;
  date_to: string;
  outcome: string;
  entry_type: string;
  tag: string;
}

export interface ReflectionAssist {
  questions: string[];
  draft: Narrative | null;
  saved: boolean;
}

export interface Analytics {
  analytics: Record<string, unknown> & {
    period: { from: string; to: string };
    closed_trades: { total: number; winning: number; losing: number; breakeven: number; unknown: number; win_share_pct: string | null };
    caveats: string[];
  };
  deterministic: { kind: string; generator_version: string; text: string; sections: Section[] };
}

export interface InstrumentOption {
  id: number;
  symbol: string;
}
