import { Card } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { DirectionBadge, TradeStatusBadge } from '@/components/shared/StatusBadge';
import { cn, formatINR, formatPrice } from '@/lib/utils';
import { useAppStore } from '@/store/useAppStore';

export function PositionsPanel() {
  const trades = useAppStore((s) => s.trades);
  const paperOrders = useAppStore((s) => s.paperOrders);
  const closeTradePosition = useAppStore((s) => s.closeTradePosition);
  const numberFormat = useAppStore((s) => s.settings.regional.numberFormat);

  const openPositions = trades.filter((t) => t.status === 'Open');

  return (
    <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
      <Card className="p-5">
        <div className="mb-3 flex items-center justify-between">
          <div>
            <h3 className="text-[15px] font-semibold text-[#18181B]">Open Paper Positions</h3>
            <p className="text-[11.5px] text-[#71717A]">
              Active automated and manual positions
            </p>
          </div>
          <span className="rounded-md bg-[#F1F5F9] px-2 py-0.5 text-[11px] font-medium text-[#475569]">
            {openPositions.length} Open
          </span>
        </div>

        <div className="overflow-hidden rounded-lg border border-[#E4E4E7]">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>ID</TableHead>
                <TableHead>Symbol</TableHead>
                <TableHead>Side</TableHead>
                <TableHead className="text-right">Qty</TableHead>
                <TableHead className="text-right">Entry</TableHead>
                <TableHead className="text-right">Net P&amp;L</TableHead>
                <TableHead className="text-right">Action</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {openPositions.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={7} className="py-8 text-center text-xs text-[#71717A]">
                    No open paper positions. Place a simulated order on the Order Ticket.
                  </TableCell>
                </TableRow>
              ) : (
                openPositions.map((pos) => {
                  const pnl = pos.netPnl ?? 0;
                  return (
                    <TableRow key={pos.id}>
                      <TableCell className="font-mono text-[11.5px] text-[#71717A]">
                        {pos.id}
                      </TableCell>
                      <TableCell className="font-semibold">{pos.instrument}</TableCell>
                      <TableCell>
                        <DirectionBadge direction={pos.direction} />
                      </TableCell>
                      <TableCell className="text-right tabular-nums">{pos.quantity}</TableCell>
                      <TableCell className="text-right tabular-nums">
                        {formatPrice(pos.entryPrice, 2, numberFormat)}
                      </TableCell>
                      <TableCell
                        className={cn(
                          'text-right font-medium tabular-nums',
                          pnl >= 0 ? 'text-[#16A34A]' : 'text-[#DC2626]'
                        )}
                      >
                        {formatINR(pnl, { showSign: true, numberFormat })}
                      </TableCell>
                      <TableCell className="text-right">
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => closeTradePosition(pos.id)}
                          className="h-7 px-2.5 text-[11.5px]"
                        >
                          Close
                        </Button>
                      </TableCell>
                    </TableRow>
                  );
                })
              )}
            </TableBody>
          </Table>
        </div>
      </Card>

      <Card className="p-5">
        <div className="mb-3 flex items-center justify-between">
          <div>
            <h3 className="text-[15px] font-semibold text-[#18181B]">Recent Manual Orders</h3>
            <p className="text-[11.5px] text-[#71717A]">
              Session paper order blotter
            </p>
          </div>
          <span className="rounded-md bg-[#F4F4F5] px-2 py-0.5 text-[11px] font-medium text-[#52525B]">
            {paperOrders.length} Orders
          </span>
        </div>

        <div className="overflow-hidden rounded-lg border border-[#E4E4E7]">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Order ID</TableHead>
                <TableHead>Symbol</TableHead>
                <TableHead>Type</TableHead>
                <TableHead className="text-right">Qty</TableHead>
                <TableHead className="text-right">Fill</TableHead>
                <TableHead className="text-right">Charges</TableHead>
                <TableHead>Status</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {paperOrders.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={7} className="py-8 text-center text-xs text-[#71717A]">
                    No manual orders placed yet in this session.
                  </TableCell>
                </TableRow>
              ) : (
                paperOrders.map((ord) => (
                  <TableRow key={ord.id}>
                    <TableCell className="font-mono text-[11.5px] text-[#71717A]">
                      {ord.id}
                    </TableCell>
                    <TableCell className="font-semibold">
                      {ord.side} {ord.symbol}
                    </TableCell>
                    <TableCell className="text-[#52525B]">{ord.orderType}</TableCell>
                    <TableCell className="text-right tabular-nums">{ord.quantity}</TableCell>
                    <TableCell className="text-right tabular-nums">
                      {formatPrice(ord.fillPrice, 2, numberFormat)}
                    </TableCell>
                    <TableCell className="text-right text-[#71717A] tabular-nums">
                      ₹{formatPrice(ord.fees + ord.slippageCost, 2, numberFormat)}
                    </TableCell>
                    <TableCell>
                      <TradeStatusBadge status={ord.status === 'Filled' ? 'Closed' : 'Open'} />
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </div>
      </Card>
    </div>
  );
}
