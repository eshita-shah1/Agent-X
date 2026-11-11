import { create } from 'zustand';
import { MOCK_INSTRUMENTS } from '@/data/mockInstruments';
import { MOCK_TRADES } from '@/data/mockTrades';
import { MOCK_JOURNAL_REPORTS } from '@/data/mockJournal';
import { DEFAULT_RISK_LIMITS } from '@/data/mockRisk';
import type {
  AgentStatus,
  AgentTrade,
  AppSettings,
  ChartTimeframe,
  ChatMessage,
  PaperOrder,
  RiskLimits,
  ToastMessage,
} from '@/types/trading';

const INITIAL_SETTINGS: AppSettings = {
  profile: {
    displayName: 'Alex Morgan',
    workspace: 'Personal workspace',
    email: 'alex.morgan@example.com',
  },
  regional: {
    timezone: 'Asia/Kolkata · IST (UTC+5:30)',
    currency: 'INR',
    appearance: 'Light',
    numberFormat: 'Indian',
  },
  paperAccount: {
    initialCapital: 1000000,
    defaultQuantity: 30,
  },
  execution: {
    transactionCostPct: 0.03,
    slippagePct: 0.05,
    orderType: 'Market',
    confirmBeforeTrade: true,
  },
  notifications: {
    riskLimitAlerts: true,
    tradeExecutionUpdates: true,
    dailySummary: false,
  },
  privacy: {
    aiTradeInsights: true,
  },
};

function buildInitialChats(): Record<string, ChatMessage[]> {
  const map: Record<string, ChatMessage[]> = {};
  for (const report of MOCK_JOURNAL_REPORTS) {
    map[report.tradeId] = [...report.initialMessages];
  }
  return map;
}

interface AppState {
  sidebarCollapsed: boolean;
  mobileNavOpen: boolean;
  selectedInstrumentSymbol: string;
  chartTimeframe: ChartTimeframe;
  agentStatus: AgentStatus;
  trades: AgentTrade[];
  paperOrders: PaperOrder[];
  availableBalance: number;
  realisedPnl: number;
  riskLimits: RiskLimits;
  settings: AppSettings;
  journalChats: Record<string, ChatMessage[]>;
  toasts: ToastMessage[];

  toggleSidebar: () => void;
  setMobileNavOpen: (open: boolean) => void;
  setSelectedInstrument: (symbol: string) => void;
  setChartTimeframe: (tf: ChartTimeframe) => void;
  startAgent: () => void;
  pauseAgent: () => void;
  triggerKillSwitch: () => void;
  executeSimulatedAgentTrade: () => void;
  placePaperOrder: (order: Omit<PaperOrder, 'id' | 'timestamp'>) => void;
  closeTradePosition: (tradeId: string) => void;
  updateRiskLimits: (limits: RiskLimits) => void;
  updateSettings: (next: AppSettings) => void;
  resetPaperAccount: () => void;
  appendJournalMessage: (tradeId: string, msg: ChatMessage) => void;
  clearJournalChat: (tradeId: string) => void;
  pushToast: (toast: Omit<ToastMessage, 'id'>) => void;
  dismissToast: (id: string) => void;
}

export const useAppStore = create<AppState>((set, get) => ({
  sidebarCollapsed: false,
  mobileNavOpen: false,
  selectedInstrumentSymbol: 'BANKNIFTY',
  chartTimeframe: '1D',
  agentStatus: 'idle',
  trades: MOCK_TRADES,
  paperOrders: [],
  availableBalance: 210180,
  realisedPnl: 10180,
  riskLimits: DEFAULT_RISK_LIMITS,
  settings: INITIAL_SETTINGS,
  journalChats: buildInitialChats(),
  toasts: [],

  toggleSidebar: () =>
    set((state) => ({ sidebarCollapsed: !state.sidebarCollapsed })),

  setMobileNavOpen: (open) => set({ mobileNavOpen: open }),

  setSelectedInstrument: (symbol) => set({ selectedInstrumentSymbol: symbol }),

  setChartTimeframe: (tf) => set({ chartTimeframe: tf }),

  startAgent: () => {
    set({ agentStatus: 'running' });
    get().pushToast({
      title: 'AgentX execution started',
      description: 'Automated paper strategies are now monitoring live simulated signals.',
      variant: 'success',
    });
  },

  pauseAgent: () => {
    set({ agentStatus: 'idle' });
    get().pushToast({
      title: 'Agent paused',
      description: 'Automated entries paused. Open positions remain monitored.',
      variant: 'default',
    });
  },

  triggerKillSwitch: () => {
    const state = get();
    let closedCount = 0;
    let deltaPnl = 0;

    const updatedTrades: AgentTrade[] = state.trades.map((trade) => {
      if (trade.status !== 'Open') return trade;
      closedCount += 1;
      const inst = MOCK_INSTRUMENTS.find((i) => i.symbol === trade.instrument);
      const exitPrice = inst?.lastPrice ?? trade.entryPrice;
      const pnl = trade.netPnl ?? 0;
      deltaPnl += pnl;
      return {
        ...trade,
        exitPrice,
        netPnl: pnl,
        status: 'Closed',
        timeInTrade: 'Halted',
      };
    });

    set({
      agentStatus: 'halted',
      trades: updatedTrades,
      realisedPnl: state.realisedPnl + deltaPnl,
      availableBalance: state.availableBalance + deltaPnl,
    });

    get().pushToast({
      title: 'Kill Switch activated',
      description:
        closedCount > 0
          ? `Agent halted immediately and ${closedCount} open paper position${closedCount > 1 ? 's' : ''} flattened.`
          : 'Agent halted immediately. All automated order routing suspended.',
      variant: 'destructive',
    });
  },

  executeSimulatedAgentTrade: () => {
    const state = get();
    if (state.agentStatus !== 'running') return;
    const inst =
      MOCK_INSTRUMENTS.find((i) => i.symbol === state.selectedInstrumentSymbol) ??
      MOCK_INSTRUMENTS[1];
    const nextNum = state.trades.length + 1;
    const id = `#TR-${String(nextNum).padStart(4, '0')}`;
    const isLong = inst.changePercent >= 0;
    const qty = state.settings.paperAccount.defaultQuantity || 30;
    const entry = inst.lastPrice;
    const stopLoss = Number((isLong ? entry * 0.995 : entry * 1.005).toFixed(2));
    const target = Number((isLong ? entry * 1.01 : entry * 0.99).toFixed(2));
    const now = new Date();
    const timeStr = `${now.toTimeString().slice(0, 8)} IST`;

    const newTrade: AgentTrade = {
      id,
      instrument: inst.symbol,
      instrumentName: inst.name,
      direction: isLong ? 'Long' : 'Short',
      strategy: 'Breakout v2',
      entryTime: timeStr,
      entryDate: '30 Nov 2026',
      entryPrice: entry,
      quantity: qty,
      stopLoss,
      target,
      exitPrice: null,
      netPnl: 0,
      status: 'Open',
      dateBucket: 'today',
      timeInTrade: 'Active',
    };

    set({ trades: [newTrade, ...state.trades] });
    if (state.settings.notifications.tradeExecutionUpdates) {
      get().pushToast({
        title: `Agent opened ${newTrade.direction} ${inst.symbol}`,
        description: `${id} · ${qty} units @ ₹${entry.toLocaleString('en-IN')}`,
        variant: 'success',
      });
    }
  },

  placePaperOrder: (orderInput) => {
    const state = get();
    const orderId = `#ORD-${String(state.paperOrders.length + 101)}`;
    const nowStr = `${new Date().toTimeString().slice(0, 8)} IST`;
    const newOrder: PaperOrder = {
      ...orderInput,
      id: orderId,
      timestamp: nowStr,
    };

    const nextTradeNum = state.trades.length + 1;
    const tradeId = `#TR-${String(nextTradeNum).padStart(4, '0')}`;
    const isLong = orderInput.side === 'Buy';
    const entry = orderInput.fillPrice;
    const sl =
      orderInput.stopLoss ??
      Number((isLong ? entry * 0.992 : entry * 1.008).toFixed(2));
    const tgt =
      orderInput.target ??
      Number((isLong ? entry * 1.016 : entry * 0.984).toFixed(2));

    const newTrade: AgentTrade = {
      id: tradeId,
      instrument: orderInput.symbol,
      instrumentName: orderInput.instrumentName,
      direction: isLong ? 'Long' : 'Short',
      strategy: `Manual (${orderInput.orderType})`,
      entryTime: nowStr,
      entryDate: '30 Nov 2026',
      entryPrice: entry,
      quantity: orderInput.quantity,
      stopLoss: sl,
      target: tgt,
      exitPrice: null,
      netPnl: -Math.round(orderInput.fees + orderInput.slippageCost),
      status: orderInput.status === 'Pending' ? 'Open' : 'Open',
      dateBucket: 'today',
      timeInTrade: 'Active',
    };

    set({
      paperOrders: [newOrder, ...state.paperOrders],
      trades: [newTrade, ...state.trades],
      availableBalance: Math.max(
        0,
        Math.round(state.availableBalance - orderInput.fees - orderInput.slippageCost)
      ),
    });

    get().pushToast({
      title: `Paper order ${orderInput.status.toLowerCase()}: ${orderInput.side} ${orderInput.quantity} ${orderInput.symbol}`,
      description: `${orderId} @ ₹${entry.toLocaleString('en-IN', { minimumFractionDigits: 2 })}`,
      variant: 'success',
    });
  },

  closeTradePosition: (tradeId) => {
    const state = get();
    const targetTrade = state.trades.find((t) => t.id === tradeId);
    if (!targetTrade || targetTrade.status !== 'Open') return;

    const inst = MOCK_INSTRUMENTS.find((i) => i.symbol === targetTrade.instrument);
    const exitPrice = inst?.lastPrice ?? targetTrade.entryPrice;
    const pnl = targetTrade.netPnl ?? 0;

    set({
      trades: state.trades.map((t) =>
        t.id === tradeId
          ? {
              ...t,
              exitPrice,
              netPnl: pnl,
              status: 'Closed',
              timeInTrade: 'Closed manually',
            }
          : t
      ),
      realisedPnl: state.realisedPnl + pnl,
      availableBalance: state.availableBalance + pnl,
    });

    get().pushToast({
      title: `Position ${tradeId} closed`,
      description: `${targetTrade.instrument} closed at ₹${exitPrice.toLocaleString('en-IN')}`,
      variant: 'default',
    });
  },

  updateRiskLimits: (limits) => {
    set({ riskLimits: limits });
    get().pushToast({
      title: 'Risk limits updated',
      description: `Risk/trade set to ${limits.riskPerTradePct.toFixed(2)}% · Max drawdown ${limits.maxDrawdownLimitPct.toFixed(2)}%`,
      variant: 'success',
    });
  },

  updateSettings: (next) => {
    set({ settings: next });
    get().pushToast({
      title: 'Settings saved',
      description: 'Workspace and execution defaults updated for upcoming simulations.',
      variant: 'success',
    });
  },

  resetPaperAccount: () => {
    const capital = get().settings.paperAccount.initialCapital;
    set({
      agentStatus: 'idle',
      trades: MOCK_TRADES,
      paperOrders: [],
      availableBalance: Math.round(capital * 0.21018),
      realisedPnl: 10180,
    });
    get().pushToast({
      title: 'Paper account reset',
      description: `Virtual capital restored to ₹${capital.toLocaleString('en-IN')}.`,
      variant: 'warning',
    });
  },

  appendJournalMessage: (tradeId, msg) =>
    set((state) => ({
      journalChats: {
        ...state.journalChats,
        [tradeId]: [...(state.journalChats[tradeId] ?? []), msg],
      },
    })),

  clearJournalChat: (tradeId) =>
    set((state) => ({
      journalChats: {
        ...state.journalChats,
        [tradeId]: [],
      },
    })),

  pushToast: (toast) => {
    const id = `toast-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
    set((state) => ({
      toasts: [...state.toasts.slice(-3), { ...toast, id }],
    }));
    setTimeout(() => {
      get().dismissToast(id);
    }, 4200);
  },

  dismissToast: (id) =>
    set((state) => ({
      toasts: state.toasts.filter((t) => t.id !== id),
    })),
}));
