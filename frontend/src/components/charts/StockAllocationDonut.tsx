import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from 'recharts';
import { Card } from '@/components/ui/card';
import { formatINR } from '@/lib/utils';
import type { PortfolioHolding } from '@/types/trading';

const ALLOCATION_COLORS = ['#4A5C72', '#6E7E91', '#8E9CAD', '#B2BDC9', '#D5DCE3'];

interface StockAllocationDonutProps {
  holdings: PortfolioHolding[];
  numberFormat?: 'Indian' | 'International';
}

export function StockAllocationDonut({
  holdings,
  numberFormat = 'Indian',
}: StockAllocationDonutProps) {
  const totalHoldingsValue = holdings.reduce((acc, h) => acc + h.currentValue, 0);

  return (
    <Card className="p-5">
      <div className="mb-3">
        <h3 className="text-[15px] font-semibold text-[#18181B]">Stock allocation</h3>
        <p className="mt-0.5 text-[11.5px] text-[#71717A]">
          By current holding value · Cash excluded
        </p>
      </div>

      <div className="flex flex-col items-center gap-6 sm:flex-row sm:justify-between">
        <div className="relative h-[200px] w-[200px] shrink-0">
          <ResponsiveContainer width="100%" height="100%">
            <PieChart>
              <Pie
                data={holdings}
                dataKey="currentValue"
                nameKey="symbol"
                cx="50%"
                cy="50%"
                innerRadius={58}
                outerRadius={82}
                paddingAngle={2}
                startAngle={90}
                endAngle={-270}
                stroke="none"
              >
                {holdings.map((entry, idx) => (
                  <Cell
                    key={entry.symbol}
                    fill={ALLOCATION_COLORS[idx % ALLOCATION_COLORS.length]}
                  />
                ))}
              </Pie>
              <Tooltip
                contentStyle={{
                  backgroundColor: '#FFFFFF',
                  border: '1px solid #E4E4E7',
                  borderRadius: '8px',
                  fontSize: '12px',
                }}
                formatter={(value: number, name: string) => [
                  formatINR(value, { numberFormat }),
                  name,
                ]}
              />
            </PieChart>
          </ResponsiveContainer>

          <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center text-center">
            <span className="text-[10.5px] text-[#71717A]">Holdings value</span>
            <span className="mt-0.5 text-[15px] font-semibold text-[#18181B] tabular-nums">
              {formatINR(totalHoldingsValue, { numberFormat })}
            </span>
          </div>
        </div>

        <div className="w-full flex-1 space-y-3 sm:pr-4">
          {holdings.map((item, idx) => (
            <div
              key={item.symbol}
              className="flex items-center justify-between text-[12.5px]"
            >
              <div className="flex items-center gap-2.5">
                <span
                  className="h-2.5 w-2.5 rounded-[2px]"
                  style={{
                    backgroundColor: ALLOCATION_COLORS[idx % ALLOCATION_COLORS.length],
                  }}
                />
                <span className="font-medium text-[#18181B]">{item.symbol}</span>
              </div>
              <span className="font-medium text-[#71717A] tabular-nums">
                {item.allocationPercent.toFixed(1)}%
              </span>
            </div>
          ))}
        </div>
      </div>
    </Card>
  );
}
