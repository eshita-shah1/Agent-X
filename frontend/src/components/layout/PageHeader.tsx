import type { ReactNode } from 'react';
import { Menu } from 'lucide-react';
import { useAppStore } from '@/store/useAppStore';
import { cn } from '@/lib/utils';

interface PageHeaderProps {
  title: string;
  subtitle: string;
  actions?: ReactNode;
  className?: string;
}

export function PageHeader({ title, subtitle, actions, className }: PageHeaderProps) {
  const setMobileNavOpen = useAppStore((s) => s.setMobileNavOpen);

  return (
    <header
      className={cn(
        'flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between',
        className
      )}
    >
      <div className="flex items-start gap-3">
        <button
          type="button"
          onClick={() => setMobileNavOpen(true)}
          className="mt-0.5 rounded-lg border border-[#E4E4E7] bg-white p-1.5 text-[#52525B] hover:bg-[#F4F4F5] md:hidden"
          aria-label="Open navigation menu"
        >
          <Menu className="h-4 w-4" />
        </button>
        <div>
          <h1 className="text-[22px] font-semibold leading-tight tracking-tight text-[#18181B]">
            {title}
          </h1>
          <p className="mt-0.5 text-[12.5px] text-[#71717A]">{subtitle}</p>
        </div>
      </div>

      {actions && <div className="flex flex-wrap items-center gap-2.5">{actions}</div>}
    </header>
  );
}
