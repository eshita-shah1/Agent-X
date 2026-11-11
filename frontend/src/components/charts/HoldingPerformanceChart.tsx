import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { Card } from '@/components/ui/card';
import { cn, formatINR } from '@/lib/utils';
import type { PortfolioHolding } from '@/types/trading';

interface HoldingPerformanceChartProps {
  holdings: PortfolioHolding[];
  numberFormat?: 'Indian' | 'International';
}

export function HoldingPerformanceChart({
  holdings,
  numberFormat = 'Indian',
}: HoldingPerformanceChartProps) {
  return (
    <Card className="p-5">
      <div className="mb-3">
        <h3 className="text-[15px] font-semibold text-[#18181B]">Holding performance</h3>
        <p className="mt-0.5 text-[11.5px] text-[#71717A]">
          Unrealised return by individual stock
        </p>
      </div>

      <div className="flex items-stretch gap-3">
        <div className="h-[220px] flex-1 min-w-0">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart
              data={holdings}
              layout="vertical"
              margin={{ top: 6, right: 10, left: 6, bottom: 4 }}
            >
              <CartesianGrid horizontal={false} stroke="#F4F4F5" />
              <XAxis
                type="number"
                domain={[-6, 10]}
                ticks={[-4, 0, 4, 8]}
                tickFormatter={(v: number) => (v > 0 ? `+${v}%` : `${v}%`)}
                axisLine={false}
                tickLine={false}
                tick={{ fontSize: 11, fill: '#71717A' }}
              />
              <YAxis
                type="category"
                dataKey="symbol"
                axisLine={false}
                tickLine={false}
                width={82}
                tick={{ fontSize: 11.5, fontWeight: 500, fill: '#18181B' }}
              />
              <ReferenceLine x={0} stroke="#D4D4D8" />
              <Tooltip
                cursor={{ fill: 'rgba(244,244,245,0.6)' }}
                contentStyle={{
                  backgroundColor: '#FFFFFF',
                  border: '1px solid #E4E4E7',
                  borderRadius: '8px',
                  fontSize: '12px',
                }}
                formatter={(value: number) => [
                  `${value >= 0 ? '+' : ''}${value.toFixed(1)}%`,
                  'Return',
                ]}
              />
              <Bar dataKey="returnPercent" barSize={16} radius={[3, 3, 3, 3]}>
                {holdings.map((item) => (
                  <Cell
                    key={item.symbol}
                    fill={item.returnPercent >= 0 ? '#16A34A' : '#DC2626'}
                  />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>

        {/* Right-aligned return % and INR readout matching Figma screenshot */}
        <div className="flex w-[76px] shrink-0 flex-col justify-between py-2.5 pb-7 text-right tabular-nums">
          {holdings.map((item) => {
            const pos = item.returnPercent >= 0;
            return (
              <div key={item.symbol} className="leading-tight">
                <p
                  className={cn(
                    'text-[11.5px] font-semibold',
                    pos ? 'text-[#16A34A]' : 'text-[#DC2626]'
                  )}
                >
                  {pos ? '+' : '−'}
                  {Math.abs(item.returnPercent).toFixed(1)}%
                </p>
                <p className="text-[10px] text-[#71717A]">
                  {formatINR(item.unrealisedPnl, { showSign: true, numberFormat })}
                </p>
              </div>
            );
          })}
        </div>
      </div>
    </Card>
  );
}
