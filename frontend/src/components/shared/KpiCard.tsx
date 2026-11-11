import { cn } from '@/lib/utils';

interface KpiCardProps {
  label: string;
  value: string;
  subtext?: string;
  tone?: 'neutral' | 'positive' | 'negative' | 'warning';
  compact?: boolean;
  className?: string;
}

export function KpiCard({
  label,
  value,
  subtext,
  tone = 'neutral',
  compact = false,
  className,
}: KpiCardProps) {
  return (
    <div
      className={cn(
        'rounded-[10px] border border-[#E4E4E7] bg-white shadow-[0_1px_2px_rgba(24,24,27,0.03)]',
        compact ? 'p-3.5' : 'p-4',
        className
      )}
    >
      <p className="text-[11.5px] font-normal text-[#71717A]">{label}</p>
      <p
        className={cn(
          'mt-1.5 font-semibold tracking-tight tabular-nums',
          compact ? 'text-[18px]' : 'text-[22px]',
          tone === 'positive'
            ? 'text-[#16A34A]'
            : tone === 'negative'
              ? 'text-[#DC2626]'
              : tone === 'warning'
                ? 'text-[#D97706]'
                : 'text-[#18181B]'
        )}
      >
        {value}
      </p>
      {subtext && (
        <p className="mt-1 truncate text-[11px] text-[#71717A]">{subtext}</p>
      )}
    </div>
  );
}
