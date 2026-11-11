import { ArrowDownRight, ArrowUpRight } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import type { TradeDirection, TradeStatus } from '@/types/trading';

export function DirectionBadge({ direction }: { direction: TradeDirection }) {
  const isLong = direction === 'Long';
  return (
    <Badge variant={isLong ? 'positive' : 'negative'} className="gap-1 font-medium">
      {isLong ? (
        <ArrowUpRight className="h-3 w-3" />
      ) : (
        <ArrowDownRight className="h-3 w-3" />
      )}
      {direction}
    </Badge>
  );
}

export function TradeStatusBadge({ status }: { status: TradeStatus }) {
  if (status === 'Open') {
    return <Badge variant="slate">Open</Badge>;
  }
  if (status === 'Stopped') {
    return <Badge variant="negative">Stopped</Badge>;
  }
  return <Badge variant="default">Closed</Badge>;
}

export function RiskStatusBadge({
  status,
}: {
  status: 'Within Limits' | 'Approaching Limit' | 'Limit Breached';
}) {
  if (status === 'Limit Breached') {
    return <Badge variant="negative">Limit Breached</Badge>;
  }
  if (status === 'Approaching Limit') {
    return <Badge variant="warning">Approaching Limit</Badge>;
  }
  return <Badge variant="positive">Within Limits</Badge>;
}
