import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import { AppShell } from '@/components/layout/AppShell';
import { DashboardPage } from '@/pages/DashboardPage';
import { TradingPage } from '@/pages/TradingPage';
import { PortfolioPage } from '@/pages/PortfolioPage';
import { JournalPage } from '@/pages/JournalPage';
import { BacktestingPage } from '@/pages/BacktestingPage';
import { RiskManagementPage } from '@/pages/RiskManagementPage';
import { BenchmarkPage } from '@/pages/BenchmarkPage';
import { SettingsPage } from '@/pages/SettingsPage';

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
          <Routes>
            <Route path="/" element={<Navigate to="/dashboard" replace />} />
            <Route path="/dashboard" element={<DashboardPage />} />
            <Route path="/trading" element={<TradingPage />} />
            <Route path="/portfolio" element={<PortfolioPage />} />
            <Route path="/journal" element={<JournalPage />} />
            <Route path="/backtesting" element={<BacktestingPage />} />
            <Route path="/risk" element={<RiskManagementPage />} />
            <Route path="/benchmark" element={<BenchmarkPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="*" element={<Navigate to="/dashboard" replace />} />
          </Routes>
        </AppShell>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
