import { useEffect, useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { CheckCircle2, Info } from 'lucide-react';
import { Card } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { ConfirmDialog } from '@/components/shared/ConfirmDialog';
import { cn, formatINR, formatPrice } from '@/lib/utils';
import { useAppStore } from '@/store/useAppStore';
import type { Instrument, OrderSide, OrderType } from '@/types/trading';

const orderFormSchema = z
  .object({
    side: z.enum(['Buy', 'Sell']),
    orderType: z.enum(['Market', 'Limit', 'Stop Loss', 'Stop Limit']),
    quantity: z.coerce
      .number({ invalid_type_error: 'Enter a valid quantity' })
      .int('Quantity must be a whole number')
      .min(1, 'Minimum quantity is 1')
      .max(10000, 'Maximum single paper order is 10,000 units'),
    limitPrice: z.coerce.number().optional(),
    triggerPrice: z.coerce.number().optional(),
    stopLoss: z.coerce.number().optional(),
    target: z.coerce.number().optional(),
  })
  .superRefine((data, ctx) => {
    if (
      (data.orderType === 'Limit' || data.orderType === 'Stop Limit') &&
      (!data.limitPrice || data.limitPrice <= 0)
    ) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['limitPrice'],
        message: 'Limit price must be greater than 0',
      });
    }
    if (
      (data.orderType === 'Stop Loss' || data.orderType === 'Stop Limit') &&
      (!data.triggerPrice || data.triggerPrice <= 0)
    ) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['triggerPrice'],
        message: 'Trigger price must be greater than 0',
      });
    }
  });

type OrderFormValues = z.infer<typeof orderFormSchema>;

interface OrderTicketProps {
  instrument: Instrument;
}

export function OrderTicket({ instrument }: OrderTicketProps) {
  const settings = useAppStore((s) => s.settings);
  const availableBalance = useAppStore((s) => s.availableBalance);
  const placePaperOrder = useAppStore((s) => s.placePaperOrder);
  const numberFormat = settings.regional.numberFormat;

  const [confirmOpen, setConfirmOpen] = useState(false);
  const [pendingValues, setPendingValues] = useState<OrderFormValues | null>(null);
  const [lastExecutedSummary, setLastExecutedSummary] = useState<string | null>(null);

  const form = useForm<OrderFormValues>({
    resolver: zodResolver(orderFormSchema),
    defaultValues: {
      side: 'Buy',
      orderType: settings.execution.orderType,
      quantity: settings.paperAccount.defaultQuantity || 30,
      limitPrice: instrument.lastPrice,
      triggerPrice: Number((instrument.lastPrice * 0.995).toFixed(2)),
      stopLoss: Number((instrument.lastPrice * 0.992).toFixed(2)),
      target: Number((instrument.lastPrice * 1.016).toFixed(2)),
    },
  });

  // Refresh default limit/trigger prices when user switches instruments
  useEffect(() => {
    form.setValue('limitPrice', instrument.lastPrice);
    form.setValue('triggerPrice', Number((instrument.lastPrice * 0.995).toFixed(2)));
    form.setValue('stopLoss', Number((instrument.lastPrice * 0.992).toFixed(2)));
    form.setValue('target', Number((instrument.lastPrice * 1.016).toFixed(2)));
  }, [instrument.symbol, instrument.lastPrice, form]);

  const side = form.watch('side') as OrderSide;
  const orderType = form.watch('orderType') as OrderType;
  const quantity = Number(form.watch('quantity')) || 0;
  const limitPrice = Number(form.watch('limitPrice')) || instrument.lastPrice;

  const refPrice =
    orderType === 'Limit' || orderType === 'Stop Limit'
      ? limitPrice
      : instrument.lastPrice;

  const notionalValue = quantity * refPrice;
  const brokerageAndFees = Number(
    ((notionalValue * settings.execution.transactionCostPct) / 100).toFixed(2)
  );
  const exchangeTaxes = Number((notionalValue * 0.00018).toFixed(2));
  const slippageCost = Number(
    ((notionalValue * settings.execution.slippagePct) / 100).toFixed(2)
  );
  const totalCharges = brokerageAndFees + exchangeTaxes + slippageCost;
  const estimatedFillPrice =
    orderType === 'Market'
      ? Number(
          (
            refPrice *
            (1 + ((side === 'Buy' ? 1 : -1) * settings.execution.slippagePct) / 100)
          ).toFixed(2)
        )
      : refPrice;

  const marginRate = instrument.category === 'INDEX' ? 0.1 : 0.2;
  const estimatedMarginRequired = Math.round(notionalValue * marginRate + totalCharges);
  const exceedsBalance = estimatedMarginRequired > availableBalance;

  const executeOrder = (values: OrderFormValues) => {
    placePaperOrder({
      symbol: instrument.symbol,
      instrumentName: instrument.name,
      side: values.side,
      orderType: values.orderType,
      quantity: values.quantity,
      limitPrice: values.limitPrice,
      triggerPrice: values.triggerPrice,
      stopLoss: values.stopLoss,
      target: values.target,
      fillPrice: estimatedFillPrice,
      fees: brokerageAndFees + exchangeTaxes,
      slippageCost,
      totalValue: Math.round(notionalValue),
      status: values.orderType === 'Market' ? 'Filled' : 'Pending',
    });

    setLastExecutedSummary(
      `${values.side} ${values.quantity} ${instrument.symbol} (${values.orderType}) @ ₹${formatPrice(estimatedFillPrice, 2, numberFormat)}`
    );
  };

  const onSubmit = (values: OrderFormValues) => {
    if (settings.execution.confirmBeforeTrade) {
      setPendingValues(values);
      setConfirmOpen(true);
    } else {
      executeOrder(values);
    }
  };

  const showLimit = orderType === 'Limit' || orderType === 'Stop Limit';
  const showTrigger = orderType === 'Stop Loss' || orderType === 'Stop Limit';

  return (
    <Card className="p-5">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-[15px] font-semibold text-[#18181B]">Order Ticket</h3>
          <p className="mt-0.5 text-[11.5px] text-[#71717A]">
            Paper execution · {instrument.symbol}
          </p>
        </div>
        <span className="rounded-md bg-[#F4F4F5] px-2 py-0.5 text-[11px] font-medium text-[#52525B]">
          Virtual INR
        </span>
      </div>

      <form onSubmit={form.handleSubmit(onSubmit)} className="mt-4 space-y-4">
        {/* Buy / Sell Direction Segmented Toggle */}
        <div className="grid grid-cols-2 gap-1.5 rounded-lg border border-[#E4E4E7] bg-[#F9FAFB] p-1">
          {(['Buy', 'Sell'] as const).map((dir) => {
            const active = side === dir;
            return (
              <button
                key={dir}
                type="button"
                onClick={() => form.setValue('side', dir)}
                className={cn(
                  'rounded-md py-1.5 text-[13px] font-semibold transition-all',
                  active && dir === 'Buy'
                    ? 'bg-[#16A34A] text-white shadow-xs'
                    : active && dir === 'Sell'
                      ? 'bg-[#DC2626] text-white shadow-xs'
                      : 'text-[#52525B] hover:text-[#18181B]'
                )}
              >
                {dir}
              </button>
            );
          })}
        </div>

        {/* Order Type */}
        <div className="space-y-1.5">
          <Label>Order type</Label>
          <Select
            value={orderType}
            onValueChange={(val) => form.setValue('orderType', val as OrderType)}
          >
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="Market">Market</SelectItem>
              <SelectItem value="Limit">Limit</SelectItem>
              <SelectItem value="Stop Loss">Stop Loss (SL)</SelectItem>
              <SelectItem value="Stop Limit">Stop Limit</SelectItem>
            </SelectContent>
          </Select>
        </div>

        {/* Quantity */}
        <div className="space-y-1.5">
          <div className="flex items-center justify-between">
            <Label htmlFor="order-qty">Quantity (units)</Label>
            <span className="text-[11px] text-[#71717A]">Lot: {instrument.lotSize}</span>
          </div>
          <Input
            id="order-qty"
            type="number"
            min={1}
            step={1}
            {...form.register('quantity')}
          />
          {form.formState.errors.quantity && (
            <p className="text-[11.5px] text-[#DC2626]">
              {form.formState.errors.quantity.message}
            </p>
          )}
          <div className="flex flex-wrap gap-1.5 pt-0.5">
            {[10, 25, 50, 100].map((preset) => (
              <button
                key={preset}
                type="button"
                onClick={() => form.setValue('quantity', preset, { shouldValidate: true })}
                className="rounded border border-[#E4E4E7] bg-white px-2 py-0.5 text-[11px] font-medium text-[#52525B] hover:bg-[#F4F4F5]"
              >
                {preset}
              </button>
            ))}
          </div>
        </div>

        {/* Conditional Limit & Trigger Prices */}
        {(showLimit || showTrigger) && (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            {showLimit && (
              <div className="space-y-1.5">
                <Label htmlFor="limit-price">Limit price (₹)</Label>
                <Input
                  id="limit-price"
                  type="number"
                  step="0.05"
                  {...form.register('limitPrice')}
                />
                {form.formState.errors.limitPrice && (
                  <p className="text-[11px] text-[#DC2626]">
                    {form.formState.errors.limitPrice.message}
                  </p>
                )}
              </div>
            )}

            {showTrigger && (
              <div className="space-y-1.5">
                <Label htmlFor="trigger-price">Trigger price (₹)</Label>
                <Input
                  id="trigger-price"
                  type="number"
                  step="0.05"
                  {...form.register('triggerPrice')}
                />
                {form.formState.errors.triggerPrice && (
                  <p className="text-[11px] text-[#DC2626]">
                    {form.formState.errors.triggerPrice.message}
                  </p>
                )}
              </div>
            )}
          </div>
        )}

        {/* Optional Bracket SL & Target */}
        <div className="grid grid-cols-2 gap-3">
          <div className="space-y-1.5">
            <Label htmlFor="order-sl">Stop Loss (₹)</Label>
            <Input id="order-sl" type="number" step="0.05" {...form.register('stopLoss')} />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="order-target">Target (₹)</Label>
            <Input id="order-target" type="number" step="0.05" {...form.register('target')} />
          </div>
        </div>

        {/* Pre-Trade Cost, Fees & Slippage Breakdown */}
        <div className="space-y-2 rounded-lg border border-[#E4E4E7] bg-[#F9FAFB] p-3 text-[12px]">
          <div className="flex items-center justify-between">
            <span className="text-[#71717A]">Est. fill price</span>
            <span className="font-medium text-[#18181B] tabular-nums">
              ₹{formatPrice(estimatedFillPrice, 2, numberFormat)}
            </span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-[#71717A]">Notional value</span>
            <span className="font-medium text-[#18181B] tabular-nums">
              {formatINR(notionalValue, { numberFormat })}
            </span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-[#71717A]">
              Transaction cost ({settings.execution.transactionCostPct.toFixed(2)}%) + taxes
            </span>
            <span className="font-medium text-[#18181B] tabular-nums">
              ₹{formatPrice(brokerageAndFees + exchangeTaxes, 2, numberFormat)}
            </span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-[#71717A]">
              Est. slippage ({settings.execution.slippagePct.toFixed(2)}%)
            </span>
            <span className="font-medium text-[#18181B] tabular-nums">
              ₹{formatPrice(slippageCost, 2, numberFormat)}
            </span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-[#71717A]">
              Est. margin required ({instrument.category === 'INDEX' ? '10%' : '20%'})
            </span>
            <span className="font-medium text-[#18181B] tabular-nums">
              {formatINR(estimatedMarginRequired, { numberFormat })}
            </span>
          </div>
          <div className="flex items-center justify-between border-t border-[#E4E4E7] pt-2">
            <span className="font-medium text-[#18181B]">Available virtual balance</span>
            <span className="font-semibold text-[#18181B] tabular-nums">
              {formatINR(availableBalance, { numberFormat })}
            </span>
          </div>
        </div>

        {exceedsBalance && (
          <div className="flex items-start gap-2 rounded-lg border border-amber-200 bg-[#FFFBEB] px-3 py-2 text-[11.5px] text-[#B45309]">
            <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>
              Estimated margin exceeds available virtual balance ({formatINR(availableBalance, { numberFormat })}). Reduce quantity or adjust capital in Settings.
            </span>
          </div>
        )}

        {/* Execution Controls */}
        <div className="flex items-center gap-2 pt-1">
          <Button
            type="button"
            variant="outline"
            onClick={() => form.reset()}
            className="w-24"
          >
            Reset
          </Button>
          <Button
            type="submit"
            disabled={exceedsBalance}
            className={cn(
              'flex-1 text-white',
              side === 'Buy'
                ? 'bg-[#16A34A] hover:bg-[#15803D]'
                : 'bg-[#DC2626] hover:bg-[#B91C1C]'
            )}
          >
            Place {side} Order
          </Button>
        </div>

        {lastExecutedSummary && (
          <div className="flex items-center gap-2 rounded-lg border border-emerald-200 bg-[#ECFDF5] px-3 py-2 text-[12px] text-[#15803D]">
            <CheckCircle2 className="h-4 w-4 shrink-0" />
            <span>Submitted: {lastExecutedSummary}</span>
          </div>
        )}
      </form>

      <ConfirmDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title={`Confirm Paper ${pendingValues?.side ?? ''} Order`}
        description={
          pendingValues
            ? `${pendingValues.side} ${pendingValues.quantity} units of ${instrument.symbol} (${pendingValues.orderType}) at est. ₹${formatPrice(estimatedFillPrice, 2, numberFormat)}. Total estimated charges: ₹${formatPrice(totalCharges, 2, numberFormat)}.`
            : ''
        }
        confirmLabel={`Confirm ${pendingValues?.side ?? 'Order'}`}
        onConfirm={() => {
          if (pendingValues) executeOrder(pendingValues);
        }}
      />
    </Card>
  );
}
