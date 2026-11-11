import * as React from 'react';
import { Slot } from '@radix-ui/react-slot';
import { cva, type VariantProps } from 'class-variance-authority';
import { cn } from '@/lib/utils';

const buttonVariants = cva(
  'inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-lg text-[13px] font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#52647A]/30 disabled:pointer-events-none disabled:opacity-50',
  {
    variants: {
      variant: {
        default: 'bg-[#52647A] text-white hover:bg-[#445468] shadow-xs',
        outline: 'border border-[#E4E4E7] bg-white text-[#18181B] hover:bg-[#F9FAFB]',
        ghost: 'text-[#52525B] hover:bg-[#F4F4F5] hover:text-[#18181B]',
        dangerOutline:
          'border border-[#E4E4E7] bg-white text-[#DC2626] hover:bg-[#FEF2F2] hover:border-red-200',
        destructive: 'bg-[#DC2626] text-white hover:bg-[#B91C1C]',
        secondary: 'bg-[#F4F4F5] text-[#18181B] hover:bg-[#E4E4E7]',
      },
      size: {
        default: 'h-9 px-3.5 py-2',
        sm: 'h-8 rounded-md px-3 text-xs',
        lg: 'h-10 rounded-lg px-4',
        icon: 'h-8 w-8 rounded-lg',
      },
    },
    defaultVariants: {
      variant: 'default',
      size: 'default',
    },
  }
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, ...props }, ref) => {
    const Comp = asChild ? Slot : 'button';
    return (
      <Comp
        className={cn(buttonVariants({ variant, size, className }))}
        ref={ref}
        {...props}
      />
    );
  }
);
Button.displayName = 'Button';
