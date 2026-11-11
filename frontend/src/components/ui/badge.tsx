import * as React from 'react';
import { cva, type VariantProps } from 'class-variance-authority';
import { cn } from '@/lib/utils';

const badgeVariants = cva(
  'inline-flex items-center rounded-md border px-2 py-0.5 text-[11px] font-medium transition-colors',
  {
    variants: {
      variant: {
        default: 'border-[#E4E4E7] bg-[#F4F4F5] text-[#52525B]',
        slate: 'border-[#E2E8F0] bg-[#F1F5F9] text-[#475569]',
        positive: 'border-emerald-200/80 bg-[#ECFDF5] text-[#15803D]',
        negative: 'border-red-200/80 bg-[#FEF2F2] text-[#DC2626]',
        warning: 'border-amber-200/80 bg-[#FFFBEB] text-[#B45309]',
        outline: 'border-[#E4E4E7] bg-white text-[#52525B]',
      },
    },
    defaultVariants: {
      variant: 'default',
    },
  }
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof badgeVariants> {}

export function Badge({ className, variant, ...props }: BadgeProps) {
  return <div className={cn(badgeVariants({ variant }), className)} {...props} />;
}
