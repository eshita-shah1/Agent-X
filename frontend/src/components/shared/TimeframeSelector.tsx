import { cn } from '@/lib/utils';

interface TimeframeSelectorProps<T extends string> {
  options: readonly T[];
  value: T;
  onChange: (value: T) => void;
  className?: string;
}

export function TimeframeSelector<T extends string>({
  options,
  value,
  onChange,
  className,
}: TimeframeSelectorProps<T>) {
  return (
    <div className={cn('inline-flex items-center gap-1', className)}>
      {options.map((tf) => {
        const active = tf === value;
        return (
          <button
            key={tf}
            type="button"
            onClick={() => onChange(tf)}
            className={cn(
              'rounded-md px-2.5 py-1 text-[12px] font-medium transition-colors',
              active
                ? 'border border-[#E4E4E7] bg-[#F4F4F5] font-semibold text-[#18181B]'
                : 'border border-transparent text-[#71717A] hover:bg-[#F9FAFB] hover:text-[#18181B]'
            )}
          >
            {tf}
          </button>
        );
      })}
    </div>
  );
}
