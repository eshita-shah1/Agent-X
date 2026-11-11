import { useMemo, useState } from 'react';
import { ArrowRight, Check, SlidersHorizontal, X } from 'lucide-react';
import { PageHeader } from '@/components/layout/PageHeader';
import { PageFooter } from '@/components/layout/PageFooter';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Slider } from '@/components/ui/slider';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { KpiCard } from '@/components/shared/KpiCard';
import { RiskStatusBadge } from '@/components/shared/StatusBadge';
import { RiskScatterChart } from '@/components/charts/RiskScatterChart';
import { ExposureCapitalChart } from '@/components/charts/ExposureCapitalChart';
import { DrawdownGauge } from '@/components/charts/DrawdownGauge';
import { PnlBarChart } from '@/components/charts/PnlBarChart';
import {
  BASE_RISK_ALERTS,
  BASE_STRATEGY_RISK_ROWS,
  MOCK_EXPOSURE_SERIES,
  MOCK_SCATTER_TRADES,
  MOCK_STREAK_SERIES,
  RISK_METRICS_BASELINE,
} from '@/data/mockRisk';
import { cn, formatINR } from '@/lib/utils';
import { useAppStore } from '@/store/useAppStore';
import type { RiskAlert, RiskLimits } from '@/types/trading';

export function RiskManagementPage() {
  const riskLimits = useAppStore((s) => s.riskLimits);
  const updateRiskLimits = useAppStore((s) => s.updateRiskLimits);
  const numberFormat = useAppStore((s) => s.settings.regional.numberFormat);

  const [editingLimits, setEditingLimits] = useState(false);
  const [draftLimits, setDraftLimits] = useState<RiskLimits>(riskLimits);
  const [alertsModalOpen, setAlertsModalOpen] = useState(false);

  const activeLimits = editingLimits ? draftLimits : riskLimits;

  // Dynamically evaluate limit status against live limits
  const evaluation = useMemo(() => {
    const exposureRatio =
      RISK_METRICS_BASELINE.currentExposurePct / activeLimits.maxCapitalAllocationPct;
    const ddRatio =
      Math.abs(RISK_METRICS_BASELINE.maxDrawdownPct) /
      Math.max(0.5, Math.abs(activeLimits.maxDrawdownLimitPct));
    const dailyRatio =
      Math.abs(RISK_METRICS_BASELINE.dailyLossPct) /
      Math.max(0.25, Math.abs(activeLimits.dailyLossLimitPct));

    const maxRatio = Math.max(exposureRatio, ddRatio, dailyRatio);

    let overallStatus: 'Within Limits' | 'Approaching Limit' | 'Limit Breached' =
      'Within Limits';
    if (maxRatio >= 1.0) overallStatus = 'Limit Breached';
    else if (maxRatio >= 0.85) overallStatus = 'Approaching Limit';

    const dynamicAlerts: RiskAlert[] = [];

    if (overallStatus === 'Limit Breached') {
      dynamicAlerts.push({
        id: 'dyn-breach',
        title: 'Risk Limit Breached',
        description: `Configured thresholds exceeded (${RISK_METRICS_BASELINE.currentExposurePct.toFixed(1)}% exposure vs ${activeLimits.maxCapitalAllocationPct.toFixed(1)}% limit, or drawdown ${RISK_METRICS_BASELINE.maxDrawdownPct.toFixed(2)}% vs ${activeLimits.maxDrawdownLimitPct.toFixed(2)}%).`,
        timeAgo: 'Just now',
        severity: 'breach',
      });
    } else if (overallStatus === 'Approaching Limit') {
      dynamicAlerts.push({
        id: 'dyn-warn',
        title: 'Approaching Risk Threshold',
        description: `Current exposure (${RISK_METRICS_BASELINE.currentExposurePct.toFixed(1)}%) is above 85% of your ${activeLimits.maxCapitalAllocationPct.toFixed(1)}% cap.`,
        timeAgo: 'Just now',
        severity: 'warning',
      });
    }

    dynamicAlerts.push(...BASE_RISK_ALERTS);

    const strategyRows = BASE_STRATEGY_RISK_ROWS.map((row) => {
      const breached =
        Math.abs(row.drawdownPct) > Math.abs(activeLimits.maxDrawdownLimitPct) ||
        row.riskPerTradePct > activeLimits.riskPerTradePct;
      const approaching =
        Math.abs(row.drawdownPct) >= Math.abs(activeLimits.maxDrawdownLimitPct) * 0.8;
      return {
        ...row,
        status: breached
          ? ('Limit Breached' as const)
          : approaching
            ? ('Approaching Limit' as const)
            : ('Within Limits' as const),
      };
    });

    return { overallStatus, dynamicAlerts, strategyRows };
  }, [activeLimits]);

  const handleSaveLimits = () => {
    updateRiskLimits(draftLimits);
    setEditingLimits(false);
  };

  return (
    <div className="space-y-4">
      <PageHeader
        title="Risk Management"
        subtitle="Monitor exposure, control risk and protect your paper capital."
        actions={
          <Badge variant="slate" className="h-8 px-3.5 text-[11px] font-semibold tracking-wider">
            LAST 3 MONTHS
          </Badge>
        }
      />

      {/* Seven Top KPI Cards */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 xl:grid-cols-7">
        <KpiCard
          compact
          label="Portfolio Value"
          value={formatINR(RISK_METRICS_BASELINE.portfolioValue, { numberFormat })}
          subtext={`+${RISK_METRICS_BASELINE.portfolioChangePct.toFixed(2)}%`}
          tone="positive"
        />
        <KpiCard
          compact
          label="Total P&L"
          value={formatINR(RISK_METRICS_BASELINE.totalPnl, { showSign: true, numberFormat })}
          subtext={`+${RISK_METRICS_BASELINE.portfolioChangePct.toFixed(2)}%`}
          tone="positive"
        />
        <KpiCard
          compact
          label="Current Exposure"
          value={`${RISK_METRICS_BASELINE.currentExposurePct.toFixed(1)}%`}
          subtext={`${formatINR(RISK_METRICS_BASELINE.investedCapital, { numberFormat })} / ${formatINR(RISK_METRICS_BASELINE.baseCapital, { numberFormat })}`}
        />
        <KpiCard
          compact
          label="Daily Loss"
          value={formatINR(RISK_METRICS_BASELINE.dailyLoss, { numberFormat })}
          subtext={`${RISK_METRICS_BASELINE.dailyLossPct.toFixed(2)}% · Limit: ${activeLimits.dailyLossLimitPct.toFixed(2)}%`}
          tone="negative"
        />
        <KpiCard
          compact
          label="Max Drawdown"
          value={`${RISK_METRICS_BASELINE.maxDrawdownPct.toFixed(2)}%`}
          subtext={`Limit: ${activeLimits.maxDrawdownLimitPct.toFixed(2)}%`}
          tone="negative"
        />
        <KpiCard
          compact
          label="Risk per Trade"
          value={`${activeLimits.riskPerTradePct.toFixed(2)}%`}
          subtext="of capital"
        />
        <KpiCard
          compact
          label="Risk Limit Status"
          value={evaluation.overallStatus}
          subtext={
            evaluation.overallStatus === 'Within Limits'
              ? 'All checks passed'
              : evaluation.overallStatus === 'Approaching Limit'
                ? 'Review exposure cap'
                : 'Action required'
          }
          tone={
            evaluation.overallStatus === 'Within Limits'
              ? 'positive'
              : evaluation.overallStatus === 'Approaching Limit'
                ? 'warning'
                : 'negative'
          }
        />
      </div>

      {/* Middle Row 1: Risk vs Reward Scatter + Exposure vs Capital */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <RiskScatterChart trades={MOCK_SCATTER_TRADES} />
        <ExposureCapitalChart data={MOCK_EXPOSURE_SERIES} />
      </div>

      {/* Middle Row 2: Maximum Drawdown Gauge + Consecutive Wins/Losses */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <DrawdownGauge
          currentDrawdownPct={RISK_METRICS_BASELINE.maxDrawdownPct}
          drawdownLimitPct={activeLimits.maxDrawdownLimitPct}
          peakValue={RISK_METRICS_BASELINE.peakValue}
          troughValue={RISK_METRICS_BASELINE.troughValue}
          recoveryProgressPct={RISK_METRICS_BASELINE.recoveryProgressPct}
          numberFormat={numberFormat}
        />

        <PnlBarChart
          title="Consecutive Wins / Losses"
          subtitle="Consecutive trades"
          headerRight={
            <div className="flex items-center gap-4 text-[11.5px] text-[#52525B]">
              <span className="inline-flex items-center gap-1.5">
                <span className="h-2.5 w-2.5 rounded-[2px] bg-[#16A34A]" />
                Win Streak
              </span>
              <span className="inline-flex items-center gap-1.5">
                <span className="h-2.5 w-2.5 rounded-[2px] bg-[#DC2626]" />
                Loss Streak
              </span>
            </div>
          }
          data={MOCK_STREAK_SERIES}
          xKey="date"
          valueKey="streak"
          yFormatter={(v) => String(v)}
          tooltipFormatter={(v) =>
            v >= 0 ? `${v} consecutive wins` : `${Math.abs(v)} consecutive losses`
          }
          barSize={8}
          height={210}
        />
      </div>

      {/* Bottom Section: Risk Limits & Controls | Risk Alerts | Strategy Risk Overview */}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-12">
        {/* Risk Limits & Controls */}
        <Card className="p-5 xl:col-span-4">
          <div className="mb-4 flex items-center justify-between gap-2">
            <h3 className="text-[15px] font-semibold text-[#18181B]">
              Risk Limits &amp; Controls
            </h3>
            {editingLimits ? (
              <div className="flex items-center gap-1.5">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => {
                    setDraftLimits(riskLimits);
                    setEditingLimits(false);
                  }}
                  className="h-7 px-2 text-xs"
                >
                  <X className="h-3 w-3" />
                  Cancel
                </Button>
                <Button size="sm" onClick={handleSaveLimits} className="h-7 px-2.5 text-xs">
                  <Check className="h-3 w-3" />
                  Save
                </Button>
              </div>
            ) : (
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  setDraftLimits(riskLimits);
                  setEditingLimits(true);
                }}
                className="h-7 gap-1.5 px-2.5 text-xs"
              >
                <SlidersHorizontal className="h-3 w-3 text-[#71717A]" />
                Edit Limits
              </Button>
            )}
          </div>

          <div className="space-y-4">
            {/* Risk per Trade */}
            <div className="space-y-1.5">
              <div className="flex items-center justify-between text-[12.5px]">
                <span className="text-[#52525B]">Risk per Trade</span>
                <span className="font-semibold text-[#18181B] tabular-nums">
                  {activeLimits.riskPerTradePct.toFixed(2)}%
                </span>
              </div>
              <Slider
                value={[activeLimits.riskPerTradePct]}
                min={0.25}
                max={3.0}
                step={0.05}
                disabled={!editingLimits}
                onValueChange={([v]) =>
                  setDraftLimits((prev) => ({ ...prev, riskPerTradePct: v }))
                }
              />
              <p className="text-[11px] text-[#71717A]">Max: 3.00%</p>
            </div>

            {/* Max Capital Allocation */}
            <div className="space-y-1.5">
              <div className="flex items-center justify-between text-[12.5px]">
                <span className="text-[#52525B]">Max Capital Allocation</span>
                <span className="font-semibold text-[#18181B] tabular-nums">
                  {activeLimits.maxCapitalAllocationPct.toFixed(2)}%
                </span>
              </div>
              <Slider
                value={[activeLimits.maxCapitalAllocationPct]}
                min={20}
                max={100}
                step={1}
                disabled={!editingLimits}
                onValueChange={([v]) =>
                  setDraftLimits((prev) => ({ ...prev, maxCapitalAllocationPct: v }))
                }
              />
              <p className="text-[11px] text-[#71717A]">Max: 100%</p>
            </div>

            {/* Daily Loss Limit */}
            <div className="space-y-1.5">
              <div className="flex items-center justify-between text-[12.5px]">
                <span className="text-[#52525B]">Daily Loss Limit</span>
                <span className="font-semibold text-[#18181B] tabular-nums">
                  {activeLimits.dailyLossLimitPct.toFixed(2)}%
                </span>
              </div>
              <Slider
                value={[Math.abs(activeLimits.dailyLossLimitPct)]}
                min={0.5}
                max={5.0}
                step={0.25}
                disabled={!editingLimits}
                onValueChange={([v]) =>
                  setDraftLimits((prev) => ({ ...prev, dailyLossLimitPct: -v }))
                }
              />
              <p className="text-[11px] text-[#71717A]">Range: −0.50% to −5.00%</p>
            </div>

            {/* Max Drawdown Limit */}
            <div className="space-y-1.5">
              <div className="flex items-center justify-between text-[12.5px]">
                <span className="text-[#52525B]">Max Drawdown Limit</span>
                <span className="font-semibold text-[#18181B] tabular-nums">
                  {activeLimits.maxDrawdownLimitPct.toFixed(2)}%
                </span>
              </div>
              <Slider
                value={[Math.abs(activeLimits.maxDrawdownLimitPct)]}
                min={3}
                max={25}
                step={0.5}
                disabled={!editingLimits}
                onValueChange={([v]) =>
                  setDraftLimits((prev) => ({ ...prev, maxDrawdownLimitPct: -v }))
                }
              />
              <p className="text-[11px] text-[#71717A]">Max tolerable: −25.00%</p>
            </div>
          </div>
        </Card>

        {/* Risk Alerts */}
        <Card className="p-5 xl:col-span-3">
          <div className="mb-4 flex items-center justify-between">
            <h3 className="text-[15px] font-semibold text-[#18181B]">Risk Alerts</h3>
            <button
              type="button"
              onClick={() => setAlertsModalOpen(true)}
              className="inline-flex items-center gap-1 text-[12px] font-medium text-[#52647A] hover:text-[#18181B]"
            >
              View All
              <ArrowRight className="h-3 w-3" />
            </button>
          </div>

          <div className="space-y-4">
            {evaluation.dynamicAlerts.slice(0, 3).map((alert) => (
              <div key={alert.id} className="flex items-start gap-2.5">
                <span
                  className={cn(
                    'mt-1.5 h-2 w-2 shrink-0 rounded-full',
                    alert.severity === 'breach'
                      ? 'bg-[#DC2626]'
                      : alert.severity === 'warning'
                        ? 'bg-[#D97706]'
                        : 'bg-[#16A34A]'
                  )}
                />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center justify-between gap-2">
                    <p className="truncate text-[12.5px] font-semibold text-[#18181B]">
                      {alert.title}
                    </p>
                    <span className="shrink-0 text-[10.5px] text-[#71717A]">
                      {alert.timeAgo}
                    </span>
                  </div>
                  <p className="mt-0.5 text-[11.5px] leading-snug text-[#71717A]">
                    {alert.description}
                  </p>
                </div>
              </div>
            ))}
          </div>
        </Card>

        {/* Strategy Risk Overview */}
        <Card className="p-5 xl:col-span-5">
          <div className="mb-3 flex items-center justify-between">
            <h3 className="text-[15px] font-semibold text-[#18181B]">
              Strategy Risk Overview
            </h3>
            <span className="text-[11px] text-[#71717A]">
              Active strategies breakdown
            </span>
          </div>

          <div className="overflow-hidden rounded-lg border border-[#E4E4E7]">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Strategy</TableHead>
                  <TableHead className="text-right">Exposure</TableHead>
                  <TableHead className="text-right">Risk/Trade</TableHead>
                  <TableHead className="text-right">P&amp;L</TableHead>
                  <TableHead className="text-right">Drawdown</TableHead>
                  <TableHead>Status</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {evaluation.strategyRows.map((row) => (
                  <TableRow key={row.strategy}>
                    <TableCell className="font-medium text-[#18181B]">
                      {row.strategy}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {row.exposurePct.toFixed(1)}%
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {row.riskPerTradePct.toFixed(2)}%
                    </TableCell>
                    <TableCell
                      className={cn(
                        'text-right font-medium tabular-nums',
                        row.pnl >= 0 ? 'text-[#16A34A]' : 'text-[#DC2626]'
                      )}
                    >
                      {formatINR(row.pnl, { showSign: true, numberFormat })}
                    </TableCell>
                    <TableCell className="text-right text-[#DC2626] tabular-nums">
                      {row.drawdownPct.toFixed(2)}%
                    </TableCell>
                    <TableCell>
                      <RiskStatusBadge status={row.status} />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </Card>
      </div>

      <PageFooter leftText="Sample data · Virtual capital only · All values in INR" />

      {/* Full Risk Alerts Modal */}
      <Dialog open={alertsModalOpen} onOpenChange={setAlertsModalOpen}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>All Risk Alerts</DialogTitle>
            <DialogDescription>
              Real-time threshold evaluations for your paper trading workspace.
            </DialogDescription>
          </DialogHeader>
          <div className="max-h-80 space-y-3 overflow-y-auto pr-1">
            {evaluation.dynamicAlerts.map((alert) => (
              <div
                key={alert.id}
                className="flex items-start gap-3 rounded-lg border border-[#E4E4E7] p-3"
              >
                <span
                  className={cn(
                    'mt-1.5 h-2 w-2 shrink-0 rounded-full',
                    alert.severity === 'breach'
                      ? 'bg-[#DC2626]'
                      : alert.severity === 'warning'
                        ? 'bg-[#D97706]'
                        : 'bg-[#16A34A]'
                  )}
                />
                <div className="flex-1">
                  <div className="flex items-center justify-between">
                    <p className="text-[13px] font-semibold text-[#18181B]">
                      {alert.title}
                    </p>
                    <span className="text-[11px] text-[#71717A]">{alert.timeAgo}</span>
                  </div>
                  <p className="mt-1 text-[12px] text-[#71717A]">{alert.description}</p>
                </div>
              </div>
            ))}
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
