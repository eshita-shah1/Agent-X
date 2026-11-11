export type InstrumentCategory = 'INDEX' | 'STOCK';

export interface Instrument {
  symbol: string;
  name: string;
  exchange: 'NSE' | 'BSE';
  category: InstrumentCategory;
  lastPrice: number;
  change: number;
  changePercent: number;
  open: number;
  high: number;
  low: number;
  prevClose: number;
  volumeLabel: string;
  lotSize: number;
}

export type ChartTimeframe = '1m' | '5m' | '15m' | '1H' | '1D';

export interface CandleBar {
  time: number; // Unix seconds
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export type TradeDirection = 'Long' | 'Short';
export type TradeStatus = 'Open' | 'Closed' | 'Stopped';
export type AgentStatus = 'idle' | 'running' | 'halted';

export interface AgentTrade {
  id: string;
  instrument: string;
  instrumentName: string;
  direction: TradeDirection;
  strategy: string;
  entryTime: string;
  entryDate: string;
  entryPrice: number;
  quantity: number;
  stopLoss: number;
  target: number;
  exitPrice: number | null;
  netPnl: number | null;
  status: TradeStatus;
  dateBucket: 'today' | 'week' | 'month';
  rMultiple?: number;
  timeInTrade?: string;
}

export type OrderSide = 'Buy' | 'Sell';
export type OrderType = 'Market' | 'Limit' | 'Stop Loss' | 'Stop Limit';

export interface PaperOrder {
  id: string;
  symbol: string;
  instrumentName: string;
  side: OrderSide;
  orderType: OrderType;
  quantity: number;
  limitPrice?: number;
  triggerPrice?: number;
  stopLoss?: number;
  target?: number;
  fillPrice: number;
  fees: number;
  slippageCost: number;
  totalValue: number;
  status: 'Filled' | 'Pending' | 'Cancelled';
  timestamp: string;
}

export interface PortfolioHolding {
  symbol: string;
  name: string;
  quantity: number;
  avgCost: number;
  currentPrice: number;
  investedValue: number;
  currentValue: number;
  unrealisedPnl: number;
  returnPercent: number;
  allocationPercent: number;
}

export type PortfolioTimeframe = '1D' | '1W' | '1M' | '3M' | '6M' | '1Y' | 'All';

export interface PortfolioSeriesPoint {
  label: string;
  value: number;
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  bullets?: string[];
  closingNote?: string;
  timestamp: string;
}

export interface JournalReport {
  instrument: string;
  instrumentName: string;
  tradeId: string;
  dateLabel: string;
  timeLabel: string;
  strategy: string;
  direction: TradeDirection;
  status: TradeStatus;
  grossPnl: number;
  returnOnNotional: number;
  riskReward: string;
  timeInTrade: string;
  thesis: string;
  entryTime: string;
  entryPrice: number;
  quantity: number;
  stopPrice: number;
  plannedRisk: number;
  exitTime: string;
  exitPrice: number;
  exitReason: string;
  plannedTarget: number;
  performanceReview: string;
  insights: {
    index: string;
    title: string;
    description: string;
  }[];
  initialMessages: ChatMessage[];
  suggestedFollowUps: string[];
}

export type BacktestChartTab = 'equity' | 'comparison' | 'sharpe';

export interface BacktestConfig {
  strategy: string;
  instrumentSymbol: string;
  initialCapital: number;
  dateRange: string;
  timeframe: string;
  lookbackPeriod: number;
  stopLossPct: number;
  targetPct: number;
  slippagePct: number;
}

export interface BacktestSeriesPoint {
  date: string;
  equity: number;
  strategyReturnPct: number;
  buyHoldReturnPct: number;
  rollingSharpe: number;
  drawdownPct: number;
}

export interface PeriodPnlPoint {
  period: string;
  pnl: number;
}

export interface BacktestResult {
  id: string;
  name: string;
  config: BacktestConfig;
  netReturnPct: number;
  netProfit: number;
  maxDrawdownPct: number;
  sharpeRatio: number;
  winRatePct: number;
  wins: number;
  totalTrades: number;
  series: BacktestSeriesPoint[];
  monthlyPnl: PeriodPnlPoint[];
  createdAt: string;
}

export interface RiskLimits {
  riskPerTradePct: number;
  maxCapitalAllocationPct: number;
  dailyLossLimitPct: number;
  maxDrawdownLimitPct: number;
}

export interface RiskScatterPoint {
  id: string;
  instrument: string;
  riskPct: number;
  rewardPct: number;
  pnl: number;
  outcome: 'win' | 'loss';
}

export interface ExposurePoint {
  date: string;
  investedExposure: number;
  availableCapital: number;
  exposurePct: number;
}

export interface StreakPoint {
  date: string;
  streak: number;
}

export interface RiskAlert {
  id: string;
  title: string;
  description: string;
  timeAgo: string;
  severity: 'ok' | 'warning' | 'breach';
}

export interface StrategyRiskRow {
  strategy: string;
  exposurePct: number;
  riskPerTradePct: number;
  pnl: number;
  drawdownPct: number;
  status: 'Within Limits' | 'Approaching Limit' | 'Limit Breached';
}

export interface BenchmarkConfig {
  strategy: string;
  benchmark: string;
  dateRange: string;
  initialCapital: number;
  timeframe: string;
}

export interface BenchmarkSeriesPoint {
  date: string;
  strategyEquity: number;
  benchmarkEquity: number;
  strategyReturnPct: number;
  benchmarkReturnPct: number;
  strategyRollingSharpe: number;
  benchmarkRollingSharpe: number;
  strategyDrawdownPct: number;
  benchmarkDrawdownPct: number;
}

export interface QuantitativeMetric {
  label: string;
  strategyValue: string;
  benchmarkValue: string;
  delta: string;
  positiveBetter: boolean;
  isPositiveDelta: boolean;
}

export interface StrategyComparisonRow {
  strategy: string;
  benchmark: string;
  totalReturnPct: number;
  benchmarkReturnPct: number;
  excessReturnPp: number;
  sharpeRatio: number;
  sortinoRatio: number;
  maxDrawdownPct: number;
  alpha: number;
  beta: number;
  winRatePct: number;
}

export interface BenchmarkResult {
  config: BenchmarkConfig;
  strategyReturnPct: number;
  strategyProfit: number;
  benchmarkReturnPct: number;
  benchmarkProfit: number;
  excessReturnPp: number;
  strategySharpe: number;
  benchmarkSharpe: number;
  netMonthlyPnl: number;
  series: BenchmarkSeriesPoint[];
  monthlyPnl: PeriodPnlPoint[];
  quantitativeMetrics: QuantitativeMetric[];
  allStrategies: StrategyComparisonRow[];
}

export interface AppSettings {
  profile: {
    displayName: string;
    workspace: string;
    email: string;
  };
  regional: {
    timezone: string;
    currency: 'INR' | 'USD';
    appearance: 'Light';
    numberFormat: 'Indian' | 'International';
  };
  paperAccount: {
    initialCapital: number;
    defaultQuantity: number;
  };
  execution: {
    transactionCostPct: number;
    slippagePct: number;
    orderType: OrderType;
    confirmBeforeTrade: boolean;
  };
  notifications: {
    riskLimitAlerts: boolean;
    tradeExecutionUpdates: boolean;
    dailySummary: boolean;
  };
  privacy: {
    aiTradeInsights: boolean;
  };
}

export interface ToastMessage {
  id: string;
  title: string;
  description?: string;
  variant?: 'default' | 'success' | 'warning' | 'destructive';
}
