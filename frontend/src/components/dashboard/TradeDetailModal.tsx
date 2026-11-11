import { useNavigate } from 'react-router-dom';
import { ArrowUpRight } from 'lucide-react';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { DirectionBadge, TradeStatusBadge } from '@/components/shared/StatusBadge';
import { cn, formatINR, formatPrice } from '@/lib/utils';
import { useAppStore } from '@/store/useAppStore';
import type { AgentTrade } from '@/types/trading';

interface TradeDetailModalProps {
  trade: AgentTrade | null;
  onClose: () => void;
}

export function TradeDetailModal({ trade, onClose }: TradeDetailModalProps) {
  const navigate = useNavigate();
  const closeTradePosition = useAppStore((s) => s.closeTradePosition);
  const numberFormat = useAppStore((s) => s.settings.regional.numberFormat);

  if (!trade) return null;

  const pnl = trade.netPnl ?? 0;
  const notional = trade.entryPrice * trade.quantity;

  return (
    <Dialog open={Boolean(trade)} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <div className="flex flex-wrap items-center gap-2">
            <DialogTitle className="text-lg">
              {trade.instrument} · {trade.id}
            </DialogTitle>
            <DirectionBadge direction={trade.direction} />
            <TradeStatusBadge status={trade.status} />
          </div>
          <DialogDescription>
            {trade.instrumentName} · {trade.strategy} · {trade.entryDate} at {trade.entryTime}
          </DialogDescription>
        </DialogHeader>

        <div className="grid grid-cols-2 gap-3 rounded-lg border border-[#E4E4E7] bg-[#F9FAFB] p-3.5 text-[12.5px]">
          <div>
            <p className="text-[11px] text-[#71717A]">Entry Price</p>
            <p className="mt-0.5 font-semibold text-[#18181B] tabular-nums">
              ₹{formatPrice(trade.entryPrice, 2, numberFormat)}
            </p>
          </div>
          <div>
            <p className="text-[11px] text-[#71717A]">Exit Price</p>
            <p className="mt-0.5 font-semibold text-[#18181B] tabular-nums">
              {trade.exitPrice === null
                ? 'Active Position'
                : `₹${formatPrice(trade.exitPrice, 2, numberFormat)}`}
            </p>
          </div>
          <div>
            <p className="text-[11px] text-[#71717A]">Quantity &amp; Notional</p>
            <p className="mt-0.5 font-semibold text-[#18181B] tabular-nums">
              {trade.quantity} units ({formatINR(notional, { numberFormat })})
            </p>
          </div>
          <div>
            <p className="text-[11px] text-[#71717A]">Net P&amp;L</p>
            <p
              className={cn(
                'mt-0.5 font-semibold tabular-nums',
                pnl >= 0 ? 'text-[#16A34A]' : 'text-[#DC2626]'
              )}
            >
              {formatINR(pnl, { showSign: true, numberFormat })}
            </p>
          </div>
          <div>
            <p className="text-[11px] text-[#71717A]">Stop Loss</p>
            <p className="mt-0.5 font-medium text-[#18181B] tabular-nums">
              ₹{formatPrice(trade.stopLoss, 2, numberFormat)}
            </p>
          </div>
          <div>
            <p className="text-[11px] text-[#71717A]">Target</p>
            <p className="mt-0.5 font-medium text-[#18181B] tabular-nums">
              ₹{formatPrice(trade.target, 2, numberFormat)}
            </p>
          </div>
        </div>

        <DialogFooter className="flex-wrap">
          {trade.status === 'Open' && (
            <Button
              variant="dangerOutline"
              onClick={() => {
                closeTradePosition(trade.id);
                onClose();
              }}
            >
              Close Paper Position
            </Button>
          )}
          <Button
            variant="outline"
            onClick={() => {
              onClose();
              navigate(`/journal?instrument=${encodeURIComponent(trade.instrument)}`);
            }}
          >
            Open AI Trade Journal
            <ArrowUpRight className="h-3.5 w-3.5" />
          </Button>
          <Button onClick={onClose}>Done</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
