import { Cell, Pie, PieChart, ResponsiveContainer } from 'recharts';
import { Card } from '@/components/ui/card';
import { formatINR } from '@/lib/utils';

interface DrawdownGaugeProps {
  currentDrawdownPct: number; // e.g. -4.2
  drawdownLimitPct: number; // e.g. -10.0
  peakValue: number;
  troughValue: number;
  recoveryProgressPct: number;
  numberFormat?: 'Indian' | 'International';
}

const GAUGE_SEGMENTS = [
  { name: 'Safe', value: 40, color: '#16A34A' },
  { name: 'Elevated', value: 30, color: '#EAB308' },
  { name: 'Critical', value: 30, color: '#EF4444' },
];

export function DrawdownGauge({
  currentDrawdownPct,
  drawdownLimitPct,
  peakValue,
  troughValue,
  recoveryProgressPct,
  numberFormat = 'Indian',
}: DrawdownGaugeProps) {
  const absCurrent = Math.abs(currentDrawdownPct);
  const absLimit = Math.max(1, Math.abs(drawdownLimitPct));
  const ratio = Math.min(1, absCurrent / absLimit);
  // Needle rotates from -90 deg (left, 0%) to +90 deg (right, limit)
  const angleDeg = -90 + ratio * 180;

  return (
    <Card className="p-5">
      <div className="mb-3">
        <h3 className="text-[15px] font-semibold text-[#18181B]">Maximum Drawdown</h3>
      </div>

      <div className="grid grid-cols-1 items-center gap-6 sm:grid-cols-2">
        {/* Semi-circular gauge */}
        <div className="flex flex-col items-center">
          <div className="relative h-[130px] w-[220px]">
            <ResponsiveContainer width="100%" height="100%">
              <PieChart>
                <Pie
                  data={GAUGE_SEGMENTS}
                  dataKey="value"
                  cx="50%"
                  cy="88%"
                  startAngle={180}
                  endAngle={0}
                  innerRadius={70}
                  outerRadius={90}
                  paddingAngle={0}
                  stroke="none"
                  isAnimationActive={false}
                >
                  {GAUGE_SEGMENTS.map((seg) => (
                    <Cell key={seg.name} fill={seg.color} />
                  ))}
                </Pie>
              </PieChart>
            </ResponsiveContainer>

            {/* Needle */}
            <div
              className="pointer-events-none absolute left-1/2 bottom-[16px] h-[54px] w-[3px] -translate-x-1/2 origin-bottom rounded-full bg-[#18181B] transition-transform duration-300"
              style={{ transform: `translateX(-50%) rotate(${angleDeg}deg)` }}
            />

            {/* Center readout */}
            <div className="pointer-events-none absolute inset-x-0 bottom-1 text-center">
              <p
                className={
                  ratio >= 1
                    ? 'text-[22px] font-semibold tabular-nums text-[#DC2626]'
                    : ratio >= 0.7
                      ? 'text-[22px] font-semibold tabular-nums text-[#D97706]'
                      : 'text-[22px] font-semibold tabular-nums text-[#16A34A]'
                }
              >
                {currentDrawdownPct.toFixed(2)}%
              </p>
              <p className="text-[11px] text-[#71717A]">Current Drawdown</p>
            </div>
          </div>

          <div className="mt-1 flex w-[184px] justify-between text-[11px] text-[#71717A] tabular-nums">
            <span>0%</span>
            <span>{drawdownLimitPct.toFixed(0)}%</span>
          </div>
        </div>

        {/* Right Metrics & Recovery Bar */}
        <div className="space-y-2.5 text-[12.5px]">
          <div className="flex items-center justify-between">
            <span className="text-[#71717A]">Max Drawdown (Current)</span>
            <span className="font-semibold text-[#18181B] tabular-nums">
              {currentDrawdownPct.toFixed(2)}%
            </span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-[#71717A]">Drawdown Limit</span>
            <span className="font-semibold text-[#18181B] tabular-nums">
              {drawdownLimitPct.toFixed(2)}%
            </span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-[#71717A]">Peak Portfolio Value</span>
            <span className="font-semibold text-[#18181B] tabular-nums">
              {formatINR(peakValue, { numberFormat })}
            </span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-[#71717A]">Trough Value</span>
            <span className="font-semibold text-[#18181B] tabular-nums">
              {formatINR(troughValue, { numberFormat })}
            </span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-[#71717A]">Recovery Progress</span>
            <span className="font-semibold text-[#18181B] tabular-nums">
              {recoveryProgressPct.toFixed(1)}%
            </span>
          </div>

          <div className="h-1.5 w-full overflow-hidden rounded-full bg-[#E4E4E7]">
            <div
              className="h-full rounded-full bg-[#16A34A] transition-all"
              style={{ width: `${Math.min(100, Math.max(0, recoveryProgressPct))}%` }}
            />
          </div>
        </div>
      </div>
    </Card>
  );
}
