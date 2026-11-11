import {
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Scatter,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { Card } from '@/components/ui/card';
import { formatINR } from '@/lib/utils';
import type { RiskScatterPoint } from '@/types/trading';

interface RiskScatterChartProps {
  trades: RiskScatterPoint[];
}

const TREND_POINTS = [
  { riskPct: 0.1, trendReward: 0.65 },
  { riskPct: 2.85, trendReward: 4.35 },
];

export function RiskScatterChart({ trades }: RiskScatterChartProps) {
  const wins = trades.filter((t) => t.outcome === 'win');
  const losses = trades.filter((t) => t.outcome === 'loss');

  return (
    <Card className="p-5">
      <div className="mb-2 flex flex-wrap items-start justify-between gap-2">
        <div>
          <h3 className="text-[15px] font-semibold text-[#18181B]">
            Risk vs Reward Scatter Plot
          </h3>
          <p className="mt-0.5 text-[11.5px] text-[#71717A]">
            Risk and realised reward for each closed trade
          </p>
        </div>

        <div className="flex items-center gap-4 text-[11.5px] text-[#52525B]">
          <span className="inline-flex items-center gap-1.5">
            <span className="h-2 w-2 rounded-full bg-[#16A34A]" />
            Winning Trade
          </span>
          <span className="inline-flex items-center gap-1.5">
            <span className="h-2 w-2 rounded-full bg-[#DC2626]" />
            Losing Trade
          </span>
        </div>
      </div>

      <p className="mb-1 text-[10.5px] text-[#71717A]">Reward per Trade (%)</p>

      <div className="h-[225px] w-full">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart margin={{ top: 8, right: 16, left: -8, bottom: 14 }}>
            <CartesianGrid stroke="#F4F4F5" />
            <XAxis
              type="number"
              dataKey="riskPct"
              domain={[0, 3.0]}
              ticks={[0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0]}
              tickFormatter={(v: number) => `${v.toFixed(1)}%`}
              axisLine={{ stroke: '#E4E4E7' }}
              tickLine={false}
              tick={{ fontSize: 10.5, fill: '#71717A' }}
              label={{
                value: 'Risk per Trade (%)',
                position: 'bottom',
                offset: 0,
                style: { fontSize: 10.5, fill: '#71717A' },
              }}
            />
            <YAxis
              type="number"
              dataKey="rewardPct"
              domain={[0, 6.0]}
              ticks={[0, 1, 2, 3, 4, 5, 6]}
              tickFormatter={(v: number) => `${v.toFixed(1)}%`}
              axisLine={false}
              tickLine={false}
              tick={{ fontSize: 10.5, fill: '#71717A' }}
            />
            <Tooltip
              cursor={{ strokeDasharray: '3 3', stroke: '#CBD5E1' }}
              content={({ active, payload }) => {
                if (!active || !payload || payload.length === 0) return null;
                const pt = payload[0]?.payload as RiskScatterPoint | undefined;
                if (!pt || !pt.id) return null;
                return (
                  <div className="rounded-lg border border-[#E4E4E7] bg-white px-3 py-2 text-xs shadow-md">
                    <p className="font-semibold text-[#18181B]">
                      {pt.id} · {pt.instrument}
                    </p>
                    <p className="mt-1 text-[#71717A]">
                      Risk: <span className="font-medium text-[#18181B]">{pt.riskPct.toFixed(2)}%</span> · Reward:{' '}
                      <span className="font-medium text-[#18181B]">{pt.rewardPct.toFixed(2)}%</span>
                    </p>
                    <p
                      className={
                        pt.pnl >= 0 ? 'mt-0.5 font-medium text-[#16A34A]' : 'mt-0.5 font-medium text-[#DC2626]'
                      }
                    >
                      P&amp;L: {formatINR(pt.pnl, { showSign: true })}
                    </p>
                  </div>
                );
              }}
            />
            <Line
              data={TREND_POINTS}
              type="linear"
              dataKey="trendReward"
              stroke="#64748B"
              strokeWidth={1.3}
              strokeDasharray="4 4"
              dot={false}
              activeDot={false}
              isAnimationActive={false}
            />
            <Scatter name="Winning Trade" data={wins} fill="#16A34A" />
            <Scatter name="Losing Trade" data={losses} fill="#DC2626" />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
    </Card>
  );
}
