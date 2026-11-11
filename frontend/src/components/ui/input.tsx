import * as React from 'react';
import { cn } from '@/lib/utils';

export type InputProps = React.InputHTMLAttributes<HTMLInputElement>;

export const Input = React.forwardRef<HTMLInputElement, InputProps>(
  ({ className, type, ...props }, ref) => {
    return (
      <input
        type={type}
        className={cn(
          'flex h-9 w-full rounded-lg border border-[#E4E4E7] bg-[#F9FAFB] px-3 py-1.5 text-[13px] text-[#18181B] transition-colors placeholder:text-[#71717A] focus-visible:border-[#52647A] focus-visible:bg-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#52647A]/15 disabled:cursor-not-allowed disabled:opacity-50',
          className
        )}
        ref={ref}
        {...props}
      />
    );
  }
);
Input.displayName = 'Input';
