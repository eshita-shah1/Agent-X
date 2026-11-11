import { useState } from 'react';
import { HelpCircle } from 'lucide-react';
import { PageHeader } from '@/components/layout/PageHeader';
import { PageFooter } from '@/components/layout/PageFooter';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { AgentControlPanel } from '@/components/agent/AgentControlPanel';
import { CandlestickChart } from '@/components/charts/CandlestickChart';
import { TradeHistoryTable } from '@/components/dashboard/TradeHistoryTable';
import { TradeDetailModal } from '@/components/dashboard/TradeDetailModal';
import type { AgentTrade } from '@/types/trading';

export function DashboardPage() {
  const [helpOpen, setHelpOpen] = useState(false);
  const [selectedTrade, setSelectedTrade] = useState<AgentTrade | null>(null);

  return (
    <div className="space-y-4">
      <PageHeader
        title="Dashboard"
        subtitle="Your strategies. A risk-free proving ground."
        actions={
          <button
            type="button"
            onClick={() => setHelpOpen(true)}
            className="flex h-9 w-9 items-center justify-center rounded-lg border border-[#E4E4E7] bg-white text-[#71717A] transition-colors hover:bg-[#F9FAFB] hover:text-[#18181B]"
            aria-label="Dashboard help"
            title="About AgentX Dashboard"
          >
            <HelpCircle className="h-4 w-4" />
          </button>
        }
      />

      {/* Real agent state and controls (Phase 5 backend); never mock data */}
      <AgentControlPanel />

      {/* Main Lightweight Candlestick + Volume Chart (mock data, see docs/FRONTEND.md) */}
      <CandlestickChart height={355} />

      {/* Trade history table: mock demo data, not agent records */}
      <TradeHistoryTable onSelectTrade={setSelectedTrade} />

      <PageFooter />

      {/* Trade View Details Dialog */}
      <TradeDetailModal
        trade={selectedTrade}
        onClose={() => setSelectedTrade(null)}
      />

      {/* Help Modal */}
      <Dialog open={helpOpen} onOpenChange={setHelpOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>AgentX Paper Trading Controls</DialogTitle>
            <DialogDescription className="pt-1 leading-relaxed">
              Simulated environment — zero real capital is at risk.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2.5 text-[12.5px] leading-relaxed text-[#52525B]">
            <p>
              <strong className="text-[#18181B]">Agent control:</strong> the real agent state from the backend. Start,
              pause, resume and stop change it; the panel shows the state the backend reports. While simulated
              execution is disabled the agent runs SIGNALS_ONLY and never submits an order.
            </p>
            <p>
              <strong className="text-[#18181B]">Pause vs stop:</strong> a paused agent keeps evaluating and exiting
              but opens nothing new; a stopped agent evaluates nothing. Neither sells your positions, and their
              stop-loss/take-profit monitoring continues.
            </p>
            <p>
              <strong className="text-[#18181B]">Kill switch:</strong> blocks entries, cancels open orders and pauses
              the agent. It never liquidates positions, and releasing it does not restart the agent.
            </p>
            <p>
              <strong className="text-[#18181B]">Chart and trade history:</strong> demo data generated in the browser,
              not market data or agent records.
            </p>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
