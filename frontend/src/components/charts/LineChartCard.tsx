import type { ReactNode } from 'react';
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { Card } from '@/components/ui/card';
import { cn } from '@/lib/utils';

export interface LineSeriesDef {
  dataKey: string;
  name: string;
  color: string;
  dashed?: boolean;
  strokeWidth?: number;
}

interface LineChartCardProps<T extends object> {
  title: string;
  subtitle?: string;
  headerRight?: ReactNode;
  headerMetric?: ReactNode;
  data: T[];
  xKey: keyof T & string;
  series: LineSeriesDef[];
  yFormatter?: (value: number) => string;
  tooltipFormatter?: (value: number, name: string) => string;
  yDomain?: [number | 'auto' | 'dataMin', number | 'auto' | 'dataMax'];
  height?: number;
  showLegend?: boolean;
  endLabels?: { dataKey: string; label: string; color?: string }[];
  className?: string;
}

export function LineChartCard<T extends object>({
  title,
  subtitle,
  headerRight,
  headerMetric,
  data,
  xKey,
  series,
  yFormatter = (v) => String(v),
  tooltipFormatter,
  yDomain = ['auto', 'auto'],
  height = 240,
  showLegend = true,
  className,
}: LineChartCardProps<T>) {
  return (
    <Card className={cn('p-5', className)}>
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="text-[15px] font-semibold text-[#18181B]">{title}</h3>
          {headerMetric && <div className="mt-1.5">{headerMetric}</div>}
          {subtitle && <p className="mt-0.5 text-[11.5px] text-[#71717A]">{subtitle}</p>}
        </div>

        <div className="flex flex-wrap items-center gap-4">
          {showLegend && series.length > 0 && (
            <div className="flex flex-wrap items-center gap-4 text-[11.5px] text-[#71717A]">
              {series.map((s) => (
                <span key={s.dataKey} className="inline-flex items-center gap-1.5">
                  <span
                    className="inline-block h-[2px] w-3.5 rounded-full"
                    style={{
                      backgroundColor: s.color,
                      borderBottom: s.dashed ? `2px dashed ${s.color}` : undefined,
                    }}
                  />
                  <span>{s.name}</span>
                </span>
              ))}
            </div>
          )}
          {headerRight}
        </div>
      </div>

      <div style={{ height }} className="w-full">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 8, right: 14, left: 4, bottom: 2 }}>
            <CartesianGrid vertical={false} stroke="#F4F4F5" />
            <XAxis
              dataKey={xKey}
              axisLine={false}
              tickLine={false}
              tick={{ fontSize: 11, fill: '#71717A' }}
              minTickGap={24}
            />
            <YAxis
              domain={yDomain}
              axisLine={false}
              tickLine={false}
              tick={{ fontSize: 11, fill: '#71717A' }}
              tickFormatter={yFormatter}
              width={56}
            />
            <Tooltip
              contentStyle={{
                backgroundColor: '#FFFFFF',
                border: '1px solid #E4E4E7',
                borderRadius: '8px',
                fontSize: '12px',
                boxShadow: '0 4px 12px rgba(24,24,27,0.06)',
              }}
              formatter={(value: number, name: string) =>
                tooltipFormatter ? [tooltipFormatter(value, name), name] : [yFormatter(value), name]
              }
            />
            {series.map((s) => (
              <Line
                key={s.dataKey}
                type="monotone"
                dataKey={s.dataKey}
                name={s.name}
                stroke={s.color}
                strokeWidth={s.strokeWidth ?? 1.75}
                strokeDasharray={s.dashed ? '4 4' : undefined}
                dot={false}
                activeDot={{ r: 3.5, strokeWidth: 0 }}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
    </Card>
  );
}
