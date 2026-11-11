import type { ReactNode } from 'react';
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { Card } from '@/components/ui/card';
import { cn } from '@/lib/utils';

export interface DrawdownSeriesDef {
  dataKey: string;
  name: string;
  color: string;
  dashed?: boolean;
  fillOpacity?: number;
}

interface AreaDrawdownChartProps<T extends object> {
  title?: string;
  subtitle?: string;
  headerRight?: ReactNode;
  data: T[];
  xKey: keyof T & string;
  series: DrawdownSeriesDef[];
  height?: number;
  className?: string;
}

export function AreaDrawdownChart<T extends object>({
  title = 'Drawdown',
  subtitle = 'Decline from previous equity peak · %',
  headerRight,
  data,
  xKey,
  series,
  height = 200,
  className,
}: AreaDrawdownChartProps<T>) {
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
          <AreaChart data={data} margin={{ top: 8, right: 12, left: 0, bottom: 2 }}>
            <defs>
              {series.map((s) => (
                <linearGradient key={s.dataKey} id={`dd-${s.dataKey}`} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor={s.color} stopOpacity={s.fillOpacity ?? 0.14} />
                  <stop offset="95%" stopColor={s.color} stopOpacity={0.02} />
                </linearGradient>
              ))}
            </defs>
            <CartesianGrid vertical={false} stroke="#F4F4F5" />
            <XAxis
              dataKey={xKey}
              axisLine={false}
              tickLine={false}
              tick={{ fontSize: 11, fill: '#71717A' }}
              minTickGap={24}
            />
            <YAxis
              axisLine={false}
              tickLine={false}
              tick={{ fontSize: 11, fill: '#71717A' }}
              tickFormatter={(v: number) => `${v}%`}
              width={46}
            />
            <Tooltip
              contentStyle={{
                backgroundColor: '#FFFFFF',
                border: '1px solid #E4E4E7',
                borderRadius: '8px',
                fontSize: '12px',
              }}
              formatter={(value: number, name: string) => [`${value.toFixed(2)}%`, name]}
            />
            {series.map((s) => (
              <Area
                key={s.dataKey}
                type="linear"
                dataKey={s.dataKey}
                name={s.name}
                stroke={s.color}
                strokeWidth={1.6}
                strokeDasharray={s.dashed ? '4 4' : undefined}
                fill={s.dashed ? 'none' : `url(#dd-${s.dataKey})`}
                dot={false}
              />
            ))}
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </Card>
  );
}
