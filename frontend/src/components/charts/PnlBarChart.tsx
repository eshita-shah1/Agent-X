import type { ReactNode } from 'react';
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

interface PnlBarChartProps<T extends object> {
  title: string;
  subtitle?: string;
  headerRight?: ReactNode;
  data: T[];
  xKey: keyof T & string;
  valueKey: keyof T & string;
  yFormatter?: (value: number) => string;
  tooltipFormatter?: (value: number) => string;
  height?: number;
  barSize?: number;
  className?: string;
}

export function PnlBarChart<T extends object>({
  title,
  subtitle,
  headerRight,
  data,
  xKey,
  valueKey,
  yFormatter = (v) => (v >= 0 ? `₹${Math.round(v / 1000)}k` : `−₹${Math.abs(Math.round(v / 1000))}k`),
  tooltipFormatter = (v) => formatINR(v, { showSign: true }),
  height = 200,
  barSize = 26,
  className,
}: PnlBarChartProps<T>) {
  return (
    <Card className={cn('p-5', className)}>
      <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="text-[15px] font-semibold text-[#18181B]">{title}</h3>
          {subtitle && <p className="mt-0.5 text-[11.5px] text-[#71717A]">{subtitle}</p>}
        </div>
        {headerRight}
      </div>

      <div style={{ height }} className="w-full">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={data} margin={{ top: 8, right: 12, left: 0, bottom: 2 }}>
            <CartesianGrid vertical={false} stroke="#F4F4F5" />
            <XAxis
              dataKey={xKey}
              axisLine={false}
              tickLine={false}
              tick={{ fontSize: 11, fill: '#71717A' }}
              minTickGap={16}
            />
            <YAxis
              axisLine={false}
              tickLine={false}
              tick={{ fontSize: 11, fill: '#71717A' }}
              tickFormatter={yFormatter}
              width={48}
            />
            <ReferenceLine y={0} stroke="#E4E4E7" />
            <Tooltip
              cursor={{ fill: 'rgba(244,244,245,0.6)' }}
              contentStyle={{
                backgroundColor: '#FFFFFF',
                border: '1px solid #E4E4E7',
                borderRadius: '8px',
                fontSize: '12px',
              }}
              formatter={(value: number) => [tooltipFormatter(value), 'Result']}
            />
            <Bar dataKey={valueKey} maxBarSize={barSize} radius={[3, 3, 3, 3]}>
              {data.map((entry, idx) => {
                const raw = Number((entry as Record<string, unknown>)[valueKey] ?? 0);
                return (
                  <Cell
                    key={`cell-${idx}`}
                    fill={raw >= 0 ? '#16A34A' : '#DC2626'}
                  />
                );
              })}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
    </Card>
  );
}
