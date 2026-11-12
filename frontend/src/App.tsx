import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { lazy, Suspense } from 'react';
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import { AppShell } from '@/components/layout/AppShell';

// Each page is loaded when its route is first opened (Phase 8): the charting libraries of the trading, backtesting and
// benchmark pages are no longer part of every page's initial download.
const DashboardPage = lazy(() => import('@/pages/DashboardPage').then((m) => ({ default: m.DashboardPage })));
const TradingPage = lazy(() => import('@/pages/TradingPage').then((m) => ({ default: m.TradingPage })));
const PortfolioPage = lazy(() => import('@/pages/PortfolioPage').then((m) => ({ default: m.PortfolioPage })));
const JournalPage = lazy(() => import('@/pages/JournalPage').then((m) => ({ default: m.JournalPage })));
const InsightsPage = lazy(() => import('@/pages/InsightsPage').then((m) => ({ default: m.InsightsPage })));
const BacktestingPage = lazy(() => import('@/pages/BacktestingPage').then((m) => ({ default: m.BacktestingPage })));
const RiskManagementPage = lazy(() => import('@/pages/RiskManagementPage').then((m) => ({ default: m.RiskManagementPage })));
const BenchmarkPage = lazy(() => import('@/pages/BenchmarkPage').then((m) => ({ default: m.BenchmarkPage })));
const SettingsPage = lazy(() => import('@/pages/SettingsPage').then((m) => ({ default: m.SettingsPage })));

function PageFallback() {
  return (
    <div className="space-y-3" role="status" aria-live="polite">
      <div className="h-6 w-48 animate-pulse rounded bg-[#F4F4F5]" />
      <div className="h-64 w-full animate-pulse rounded-[10px] bg-[#F9FAFB]" />
      <span className="sr-only">Loading page…</span>
    </div>
  );
}

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AppShell>
          <Suspense fallback={<PageFallback />}>
          <Routes>
            <Route path="/" element={<Navigate to="/dashboard" replace />} />
            <Route path="/dashboard" element={<DashboardPage />} />
            <Route path="/trading" element={<TradingPage />} />
            <Route path="/portfolio" element={<PortfolioPage />} />
            <Route path="/journal" element={<JournalPage />} />
            <Route path="/insights" element={<InsightsPage />} />
            <Route path="/backtesting" element={<BacktestingPage />} />
            <Route path="/risk" element={<RiskManagementPage />} />
            <Route path="/benchmark" element={<BenchmarkPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="*" element={<Navigate to="/dashboard" replace />} />
          </Routes>
          </Suspense>
        </AppShell>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
