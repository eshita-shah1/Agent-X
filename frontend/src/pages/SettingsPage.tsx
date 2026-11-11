import { useEffect, useState } from 'react';
import { Controller, useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { PageHeader } from '@/components/layout/PageHeader';
import { PageFooter } from '@/components/layout/PageFooter';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Switch } from '@/components/ui/switch';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { ConfirmDialog } from '@/components/shared/ConfirmDialog';
import { exportRowsToCsv } from '@/lib/csvExport';
import { useAppStore } from '@/store/useAppStore';
import type { AppSettings, OrderType } from '@/types/trading';

const settingsSchema = z.object({
  profile: z.object({
    displayName: z.string().min(2, 'Display name must be at least 2 characters'),
    workspace: z.string().min(2, 'Workspace name is required'),
    email: z.string().email('Enter a valid email address'),
  }),
  regional: z.object({
    timezone: z.string(),
    currency: z.enum(['INR', 'USD']),
    appearance: z.literal('Light'),
    numberFormat: z.enum(['Indian', 'International']),
  }),
  paperAccount: z.object({
    initialCapital: z.coerce
      .number({ invalid_type_error: 'Enter a valid numeric capital amount' })
      .min(50000, 'Minimum initial virtual capital is ₹50,000')
      .max(100000000, 'Maximum virtual capital is ₹10,00,00,000'),
    defaultQuantity: z.coerce
      .number({ invalid_type_error: 'Enter a valid default quantity' })
      .int('Must be a whole number')
      .min(1, 'Minimum default quantity is 1')
      .max(5000, 'Maximum default quantity is 5,000'),
  }),
  execution: z.object({
    transactionCostPct: z.coerce
      .number()
      .min(0, 'Cannot be negative')
      .max(2, 'Cannot exceed 2%'),
    slippagePct: z.coerce
      .number()
      .min(0, 'Cannot be negative')
      .max(2, 'Cannot exceed 2%'),
    orderType: z.enum(['Market', 'Limit', 'Stop Loss', 'Stop Limit']),
    confirmBeforeTrade: z.boolean(),
  }),
  notifications: z.object({
    riskLimitAlerts: z.boolean(),
    tradeExecutionUpdates: z.boolean(),
    dailySummary: z.boolean(),
  }),
  privacy: z.object({
    aiTradeInsights: z.boolean(),
  }),
});

type SettingsTab = 'all' | 'general' | 'paper' | 'notifications';

export function SettingsPage() {
  const settings = useAppStore((s) => s.settings);
  const updateSettings = useAppStore((s) => s.updateSettings);
  const resetPaperAccount = useAppStore((s) => s.resetPaperAccount);
  const trades = useAppStore((s) => s.trades);
  const pushToast = useAppStore((s) => s.pushToast);

  const [activeTab, setActiveTab] = useState<SettingsTab>('all');
  const [resetConfirmOpen, setResetConfirmOpen] = useState(false);

  const form = useForm<AppSettings>({
    resolver: zodResolver(settingsSchema),
    defaultValues: settings,
  });

  useEffect(() => {
    form.reset(settings);
  }, [settings, form]);

  const onSubmit = (data: AppSettings) => {
    updateSettings(data);
  };

  const handleExportCsv = () => {
    const headers = [
      'Trade ID',
      'Instrument',
      'Direction',
      'Strategy',
      'Entry Date',
      'Entry Time',
      'Entry Price',
      'Quantity',
      'Stop Loss',
      'Target',
      'Exit Price',
      'Net P&L',
      'Status',
    ];
    const rows = trades.map((t) => [
      t.id,
      t.instrument,
      t.direction,
      t.strategy,
      t.entryDate,
      t.entryTime,
      t.entryPrice,
      t.quantity,
      t.stopLoss,
      t.target,
      t.exitPrice,
      t.netPnl,
      t.status,
    ]);
    exportRowsToCsv('agentx-paper-trades.csv', headers, rows);
    pushToast({
      title: 'Trading data exported',
      description: `Exported ${trades.length} paper trade records to CSV.`,
      variant: 'success',
    });
  };

  const showGeneral = activeTab === 'all' || activeTab === 'general';
  const showPaper = activeTab === 'all' || activeTab === 'paper';
  const showNotifications = activeTab === 'all' || activeTab === 'notifications';

  return (
    <div className="space-y-4">
      <PageHeader
        title="Settings"
        subtitle="Manage your workspace and paper trading preferences."
        actions={
          <Badge variant="slate" className="h-8 px-3.5 text-[11px] font-semibold tracking-wider">
            PAPER ACCOUNT
          </Badge>
        }
      />

      {/* Tabs Header Bar */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Tabs value={activeTab} onValueChange={(v) => setActiveTab(v as SettingsTab)}>
          <TabsList className="flex-wrap">
            <TabsTrigger value="all">All</TabsTrigger>
            <TabsTrigger value="general">General</TabsTrigger>
            <TabsTrigger value="paper">Paper trading</TabsTrigger>
            <TabsTrigger value="notifications">Notifications &amp; data</TabsTrigger>
          </TabsList>
        </Tabs>

        <span className="text-[11.5px] text-[#71717A]">
          Preferences apply to this workspace only.
        </span>
      </div>

      {/* Settings Form Grid */}
      <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-4">
        {showGeneral && (
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            {/* 1. Profile & Workspace */}
            <Card className="p-5">
              <h2 className="text-[15px] font-semibold text-[#18181B]">
                Profile &amp; Workspace
              </h2>
              <p className="mt-0.5 text-[11.5px] text-[#71717A]">
                Your identity in this paper trading workspace.
              </p>

              <div className="mt-4 space-y-3.5">
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                  <div className="space-y-1.5">
                    <Label htmlFor="displayName">Display name</Label>
                    <Input id="displayName" {...form.register('profile.displayName')} />
                    {form.formState.errors.profile?.displayName && (
                      <p className="text-[11px] text-[#DC2626]">
                        {form.formState.errors.profile.displayName.message}
                      </p>
                    )}
                  </div>
                  <div className="space-y-1.5">
                    <Label htmlFor="workspace">Workspace</Label>
                    <Input id="workspace" {...form.register('profile.workspace')} />
                    {form.formState.errors.profile?.workspace && (
                      <p className="text-[11px] text-[#DC2626]">
                        {form.formState.errors.profile.workspace.message}
                      </p>
                    )}
                  </div>
                </div>

                <div className="space-y-1.5">
                  <Label htmlFor="email">Email address</Label>
                  <Input id="email" type="email" {...form.register('profile.email')} />
                  {form.formState.errors.profile?.email && (
                    <p className="text-[11px] text-[#DC2626]">
                      {form.formState.errors.profile.email.message}
                    </p>
                  )}
                </div>
              </div>
            </Card>

            {/* 2. Regional Preferences */}
            <Card className="p-5">
              <h2 className="text-[15px] font-semibold text-[#18181B]">
                Regional Preferences
              </h2>
              <p className="mt-0.5 text-[11.5px] text-[#71717A]">
                Consistent market times and number formats.
              </p>

              <div className="mt-4 grid grid-cols-1 gap-3.5 sm:grid-cols-2">
                <div className="space-y-1.5">
                  <Label>Timezone</Label>
                  <Controller
                    control={form.control}
                    name="regional.timezone"
                    render={({ field }) => (
                      <Select value={field.value} onValueChange={field.onChange}>
                        <SelectTrigger className="bg-[#F9FAFB]">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="Asia/Kolkata · IST (UTC+5:30)">
                            Asia/Kolkata · IST (UTC+5:30)
                          </SelectItem>
                          <SelectItem value="UTC · Coordinated Universal Time">
                            UTC · Coordinated Universal Time
                          </SelectItem>
                          <SelectItem value="America/New_York · EST (UTC-5:00)">
                            America/New_York · EST (UTC-5:00)
                          </SelectItem>
                        </SelectContent>
                      </Select>
                    )}
                  />
                </div>

                <div className="space-y-1.5">
                  <Label>Base currency</Label>
                  <Controller
                    control={form.control}
                    name="regional.currency"
                    render={({ field }) => (
                      <Select value={field.value} onValueChange={field.onChange}>
                        <SelectTrigger className="bg-[#F9FAFB]">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="INR">INR · Indian Rupee</SelectItem>
                          <SelectItem value="USD">USD · US Dollar</SelectItem>
                        </SelectContent>
                      </Select>
                    )}
                  />
                </div>

                <div className="space-y-1.5">
                  <Label>Appearance</Label>
                  <Controller
                    control={form.control}
                    name="regional.appearance"
                    render={({ field }) => (
                      <Select value={field.value} onValueChange={field.onChange}>
                        <SelectTrigger className="bg-[#F9FAFB]">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="Light">Light</SelectItem>
                        </SelectContent>
                      </Select>
                    )}
                  />
                </div>

                <div className="space-y-1.5">
                  <Label>Number format</Label>
                  <Controller
                    control={form.control}
                    name="regional.numberFormat"
                    render={({ field }) => (
                      <Select value={field.value} onValueChange={field.onChange}>
                        <SelectTrigger className="bg-[#F9FAFB]">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="Indian">Indian · 10,00,000.00</SelectItem>
                          <SelectItem value="International">
                            International · 1,000,000.00
                          </SelectItem>
                        </SelectContent>
                      </Select>
                    )}
                  />
                </div>
              </div>
            </Card>
          </div>
        )}

        {showPaper && (
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            {/* 3. Paper Trading Account */}
            <Card className="p-5">
              <h2 className="text-[15px] font-semibold text-[#18181B]">
                Paper Trading Account
              </h2>
              <p className="mt-0.5 text-[11.5px] text-[#71717A]">
                Virtual capital only. No real orders are placed.
              </p>

              <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2">
                <div className="space-y-1.5">
                  <Label htmlFor="initialCapital">Initial virtual capital (₹)</Label>
                  <Input
                    id="initialCapital"
                    type="number"
                    {...form.register('paperAccount.initialCapital')}
                  />
                  {form.formState.errors.paperAccount?.initialCapital && (
                    <p className="text-[11px] text-[#DC2626]">
                      {form.formState.errors.paperAccount.initialCapital.message}
                    </p>
                  )}
                </div>

                <div className="space-y-1.5">
                  <Label htmlFor="defaultQuantity">Default order quantity (units)</Label>
                  <Input
                    id="defaultQuantity"
                    type="number"
                    {...form.register('paperAccount.defaultQuantity')}
                  />
                  {form.formState.errors.paperAccount?.defaultQuantity && (
                    <p className="text-[11px] text-[#DC2626]">
                      {form.formState.errors.paperAccount.defaultQuantity.message}
                    </p>
                  )}
                </div>
              </div>

              <div className="mt-5 flex flex-wrap items-center justify-between gap-3 border-t border-[#F4F4F5] pt-4">
                <div>
                  <p className="text-[13px] font-medium text-[#18181B]">
                    Reset paper account
                  </p>
                  <p className="text-[11.5px] text-[#71717A]">
                    Clears positions and restores initial virtual capital.
                  </p>
                </div>
                <Button
                  type="button"
                  variant="dangerOutline"
                  onClick={() => setResetConfirmOpen(true)}
                >
                  Reset account
                </Button>
              </div>

              <p className="mt-4 text-[11px] text-[#71717A]">
                Risk limits are managed on the Risk Management page.
              </p>
            </Card>

            {/* 4. Execution Defaults */}
            <Card className="p-5">
              <h2 className="text-[15px] font-semibold text-[#18181B]">
                Execution Defaults
              </h2>
              <p className="mt-0.5 text-[11.5px] text-[#71717A]">
                Used for new backtests and simulated orders.
              </p>

              <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-3">
                <div className="space-y-1.5">
                  <Label htmlFor="transactionCostPct">Transaction costs (%)</Label>
                  <Input
                    id="transactionCostPct"
                    type="number"
                    step="0.01"
                    {...form.register('execution.transactionCostPct')}
                  />
                  {form.formState.errors.execution?.transactionCostPct && (
                    <p className="text-[11px] text-[#DC2626]">
                      {form.formState.errors.execution.transactionCostPct.message}
                    </p>
                  )}
                </div>

                <div className="space-y-1.5">
                  <Label htmlFor="slippagePct">Slippage (%)</Label>
                  <Input
                    id="slippagePct"
                    type="number"
                    step="0.01"
                    {...form.register('execution.slippagePct')}
                  />
                  {form.formState.errors.execution?.slippagePct && (
                    <p className="text-[11px] text-[#DC2626]">
                      {form.formState.errors.execution.slippagePct.message}
                    </p>
                  )}
                </div>

                <div className="space-y-1.5">
                  <Label>Order type</Label>
                  <Controller
                    control={form.control}
                    name="execution.orderType"
                    render={({ field }) => (
                      <Select
                        value={field.value}
                        onValueChange={(v) => field.onChange(v as OrderType)}
                      >
                        <SelectTrigger className="bg-[#F9FAFB]">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="Market">Market</SelectItem>
                          <SelectItem value="Limit">Limit</SelectItem>
                          <SelectItem value="Stop Loss">Stop Loss</SelectItem>
                          <SelectItem value="Stop Limit">Stop Limit</SelectItem>
                        </SelectContent>
                      </Select>
                    )}
                  />
                </div>
              </div>

              <div className="mt-5 flex items-center justify-between gap-3 border-t border-[#F4F4F5] pt-4">
                <div>
                  <p className="text-[13px] font-medium text-[#18181B]">
                    Confirm before placing trades
                  </p>
                  <p className="text-[11.5px] text-[#71717A]">
                    Review the instrument, quantity and risk before execution.
                  </p>
                </div>
                <Controller
                  control={form.control}
                  name="execution.confirmBeforeTrade"
                  render={({ field }) => (
                    <Switch checked={field.value} onCheckedChange={field.onChange} />
                  )}
                />
              </div>

              <p className="mt-4 text-[11px] text-[#71717A]">
                No leverage · Long and short trades use virtual capital.
              </p>
            </Card>
          </div>
        )}

        {showNotifications && (
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            {/* 5. Notifications */}
            <Card className="p-5">
              <h2 className="text-[15px] font-semibold text-[#18181B]">Notifications</h2>

              <div className="mt-4 divide-y divide-[#F4F4F5]">
                <div className="flex items-center justify-between gap-3 pb-3.5">
                  <div>
                    <p className="text-[13px] font-medium text-[#18181B]">
                      Risk limit alerts
                    </p>
                    <p className="text-[11.5px] text-[#71717A]">
                      Notify when exposure or drawdown approaches a limit.
                    </p>
                  </div>
                  <Controller
                    control={form.control}
                    name="notifications.riskLimitAlerts"
                    render={({ field }) => (
                      <Switch checked={field.value} onCheckedChange={field.onChange} />
                    )}
                  />
                </div>

                <div className="flex items-center justify-between gap-3 py-3.5">
                  <div>
                    <p className="text-[13px] font-medium text-[#18181B]">
                      Trade execution updates
                    </p>
                    <p className="text-[11.5px] text-[#71717A]">
                      Notify when a simulated position opens or closes.
                    </p>
                  </div>
                  <Controller
                    control={form.control}
                    name="notifications.tradeExecutionUpdates"
                    render={({ field }) => (
                      <Switch checked={field.value} onCheckedChange={field.onChange} />
                    )}
                  />
                </div>

                <div className="flex items-center justify-between gap-3 pt-3.5">
                  <div>
                    <p className="text-[13px] font-medium text-[#18181B]">
                      Daily trading summary
                    </p>
                    <p className="text-[11.5px] text-[#71717A]">
                      Receive a recap of your paper trading session.
                    </p>
                  </div>
                  <Controller
                    control={form.control}
                    name="notifications.dailySummary"
                    render={({ field }) => (
                      <Switch checked={field.value} onCheckedChange={field.onChange} />
                    )}
                  />
                </div>
              </div>
            </Card>

            {/* 6. Data & Privacy */}
            <Card className="p-5">
              <h2 className="text-[15px] font-semibold text-[#18181B]">
                Data &amp; Privacy
              </h2>

              <div className="mt-4 divide-y divide-[#F4F4F5]">
                <div className="flex items-center justify-between gap-3 pb-4">
                  <div>
                    <p className="text-[13px] font-medium text-[#18181B]">
                      AI trade insights
                    </p>
                    <p className="text-[11.5px] text-[#71717A]">
                      Allow the trade assistant to analyse simulated executions.
                    </p>
                  </div>
                  <Controller
                    control={form.control}
                    name="privacy.aiTradeInsights"
                    render={({ field }) => (
                      <Switch checked={field.value} onCheckedChange={field.onChange} />
                    )}
                  />
                </div>

                <div className="flex items-center justify-between gap-3 py-4">
                  <div>
                    <p className="text-[13px] font-medium text-[#18181B]">
                      Export trading data
                    </p>
                    <p className="text-[11.5px] text-[#71717A]">
                      Download your trade journal and performance history.
                    </p>
                  </div>
                  <Button type="button" variant="outline" onClick={handleExportCsv}>
                    Export CSV
                  </Button>
                </div>
              </div>

              <p className="mt-3 text-[11px] text-[#71717A]">
                Sample workspace · No brokerage account connected.
              </p>
            </Card>
          </div>
        )}

        {/* Save / Discard Action Bar */}
        <div className="flex flex-wrap items-center justify-between gap-3 pt-1">
          <span className="text-[11.5px] text-[#71717A]">
            Changes apply to new simulations. Existing results remain unchanged.
          </span>
          <div className="flex items-center gap-2.5">
            <Button
              type="button"
              variant="outline"
              onClick={() => {
                form.reset(settings);
                pushToast({
                  title: 'Changes discarded',
                  description: 'Settings reverted to last saved preferences.',
                });
              }}
            >
              Discard changes
            </Button>
            <Button type="submit">Save changes</Button>
          </div>
        </div>
      </form>

      <PageFooter leftText="Sample data · Virtual capital only · All values in INR" />

      <ConfirmDialog
        open={resetConfirmOpen}
        onOpenChange={setResetConfirmOpen}
        title="Reset Paper Trading Account?"
        description="This clears session manual positions and restores your virtual capital and baseline simulation trades."
        confirmLabel="Reset Account"
        destructive
        onConfirm={resetPaperAccount}
      />
    </div>
  );
}
