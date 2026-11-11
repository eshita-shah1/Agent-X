import { useEffect, useState } from 'react';
import { HelpCircle, Pause, Play, Square } from 'lucide-react';
import { PageHeader } from '@/components/layout/PageHeader';
import { PageFooter } from '@/components/layout/PageFooter';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { CandlestickChart } from '@/components/charts/CandlestickChart';
import { TradeHistoryTable } from '@/components/dashboard/TradeHistoryTable';
import { TradeDetailModal } from '@/components/dashboard/TradeDetailModal';
import { ConfirmDialog } from '@/components/shared/ConfirmDialog';
import { useAppStore } from '@/store/useAppStore';
import type { AgentTrade } from '@/types/trading';

export function DashboardPage() {
  const agentStatus = useAppStore((s) => s.agentStatus);
  const startAgent = useAppStore((s) => s.startAgent);
  const pauseAgent = useAppStore((s) => s.pauseAgent);
  const triggerKillSwitch = useAppStore((s) => s.triggerKillSwitch);
  const executeSimulatedAgentTrade = useAppStore((s) => s.executeSimulatedAgentTrade);

  const [killConfirmOpen, setKillConfirmOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [selectedTrade, setSelectedTrade] = useState<AgentTrade | null>(null);

  // When agent is running, simulate occasional paper execution updates
  useEffect(() => {
    if (agentStatus !== 'running') return;
    const timer = setInterval(() => {
      executeSimulatedAgentTrade();
    }, 25_000);
    return () => clearInterval(timer);
  }, [agentStatus, executeSimulatedAgentTrade]);

  return (
    <div className="space-y-4">
      <PageHeader
        title="Dashboard"
        subtitle="Your strategies. A risk-free proving ground."
        actions={
          <>
            {agentStatus === 'running' ? (
              <Button variant="default" onClick={pauseAgent}>
                <Pause className="h-3.5 w-3.5" />
                Pause Agent
              </Button>
            ) : (
              <Button variant="default" onClick={startAgent}>
                <Play className="h-3.5 w-3.5" />
                Start Agent
              </Button>
            )}

            <Button
              variant="dangerOutline"
              onClick={() => setKillConfirmOpen(true)}
            >
              <Square className="h-3.5 w-3.5" />
              Kill Switch
            </Button>

            <button
              type="button"
              onClick={() => setHelpOpen(true)}
              className="flex h-9 w-9 items-center justify-center rounded-lg border border-[#E4E4E7] bg-white text-[#71717A] transition-colors hover:bg-[#F9FAFB] hover:text-[#18181B]"
              aria-label="Dashboard help"
              title="About AgentX Dashboard"
            >
              <HelpCircle className="h-4 w-4" />
            </button>
          </>
        }
      />

      {/* Main Lightweight Candlestick + Volume Chart */}
      <CandlestickChart height={355} />

      {/* 13-Column Agent Activity / Trade History Table */}
      <TradeHistoryTable onSelectTrade={setSelectedTrade} />

      <PageFooter />

      {/* Kill Switch Emergency Confirmation */}
      <ConfirmDialog
        open={killConfirmOpen}
        onOpenChange={setKillConfirmOpen}
        title="Activate Emergency Kill Switch?"
        description="This immediately halts all automated AgentX strategies and flattens open simulated positions at current market prices."
        confirmLabel="Halt Agent & Flatten"
        destructive
        onConfirm={triggerKillSwitch}
      />

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
              <strong className="text-[#18181B]">Start Agent:</strong> Activates automated paper strategies against selected NSE/BSE instruments.
            </p>
            <p>
              <strong className="text-[#18181B]">Kill Switch:</strong> Emergency halt that pauses agent execution and closes all active paper positions immediately.
            </p>
            <p>
              <strong className="text-[#18181B]">Instruments Panel:</strong> Click any index or stock on the right to inspect its OHLCV candlestick chart and volume.
            </p>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
