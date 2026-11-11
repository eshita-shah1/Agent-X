import {
  Bar,
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { Card } from '@/components/ui/card';
import { formatINR } from '@/lib/utils';
import type { ExposurePoint } from '@/types/trading';

interface ExposureCapitalChartProps {
  data: ExposurePoint[];
}

export function ExposureCapitalChart({ data }: ExposureCapitalChartProps) {
  return (
    <Card className="p-5">
      <div className="mb-2 flex flex-wrap items-start justify-between gap-2">
        <div>
          <h3 className="text-[15px] font-semibold text-[#18181B]">Exposure vs Capital</h3>
        </div>

        <div className="flex flex-wrap items-center gap-4 text-[11.5px] text-[#52525B]">
          <span className="inline-flex items-center gap-1.5">
            <span className="h-2.5 w-2.5 rounded-[2px] bg-[#94A3B8]" />
            Invested exposure (₹)
          </span>
          <span className="inline-flex items-center gap-1.5">
            <span className="h-2.5 w-2.5 rounded-[2px] bg-[#E2E8F0]" />
            Available capital (₹)
          </span>
          <span className="inline-flex items-center gap-1.5">
            <span className="h-2 w-2 rounded-full bg-[#16A34A]" />
            Exposure (%)
          </span>
        </div>
      </div>

      <p className="mb-1 text-[10.5px] text-[#71717A]">Amount (₹)</p>

      <div className="h-[235px] w-full">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={data} margin={{ top: 8, right: 4, left: 6, bottom: 2 }}>
            <CartesianGrid vertical={false} stroke="#F4F4F5" />
            <XAxis
              dataKey="date"
              axisLine={false}
              tickLine={false}
              tick={{ fontSize: 10.5, fill: '#71717A' }}
            />
            <YAxis
              yAxisId="left"
              domain={[0, 1000000]}
              ticks={[0, 200000, 400000, 600000, 800000, 1000000]}
              tickFormatter={(v: number) => v.toLocaleString('en-IN')}
              axisLine={false}
              tickLine={false}
              tick={{ fontSize: 10.5, fill: '#71717A' }}
              width={64}
            />
            <YAxis
              yAxisId="right"
              orientation="right"
              domain={[0, 100]}
              ticks={[0, 20, 40, 60, 80, 100]}
              tickFormatter={(v: number) => `${v}%`}
              axisLine={false}
              tickLine={false}
              tick={{ fontSize: 10.5, fill: '#71717A' }}
              width={38}
            />
            <Tooltip
              contentStyle={{
                backgroundColor: '#FFFFFF',
                border: '1px solid #E4E4E7',
                borderRadius: '8px',
                fontSize: '12px',
              }}
              formatter={(value: number, name: string) => {
                if (name === 'Exposure (%)') return [`${value}%`, name];
                return [formatINR(value), name];
              }}
            />
            <Bar
              yAxisId="left"
              dataKey="investedExposure"
              name="Invested exposure (₹)"
              stackId="cap"
              fill="#94A3B8"
              barSize={38}
            />
            <Bar
              yAxisId="left"
              dataKey="availableCapital"
              name="Available capital (₹)"
              stackId="cap"
              fill="#E2E8F0"
              barSize={38}
              radius={[3, 3, 0, 0]}
            />
            <Line
              yAxisId="right"
              type="monotone"
              dataKey="exposurePct"
              name="Exposure (%)"
              stroke="#16A34A"
              strokeWidth={2}
              dot={{ r: 3.5, fill: '#16A34A', strokeWidth: 0 }}
            />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
    </Card>
  );
}
