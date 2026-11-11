import type { ReactNode } from 'react';
import { AlertCircle, RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/button';

interface QueryStateWrapperProps {
  isLoading?: boolean;
  isError?: boolean;
  errorMessage?: string;
  onRetry?: () => void;
  isEmpty?: boolean;
  emptyTitle?: string;
  emptyDescription?: string;
  children: ReactNode;
}

export function QueryStateWrapper({
  isLoading,
  isError,
  errorMessage = 'Unable to load simulated market data.',
  onRetry,
  isEmpty,
  emptyTitle = 'No data available',
  emptyDescription = 'Adjust filters or parameters and try again.',
  children,
}: QueryStateWrapperProps) {
  if (isLoading) {
    return (
      <div className="space-y-3 rounded-[10px] border border-[#E4E4E7] bg-white p-5">
        <div className="h-4 w-40 animate-pulse rounded bg-[#F4F4F5]" />
        <div className="h-56 w-full animate-pulse rounded-lg bg-[#F9FAFB]" />
      </div>
    );
  }

  if (isError) {
    return (
      <div className="flex flex-col items-center justify-center rounded-[10px] border border-[#E4E4E7] bg-white px-6 py-12 text-center">
        <AlertCircle className="h-7 w-7 text-[#DC2626]" />
        <p className="mt-2.5 text-sm font-semibold text-[#18181B]">Failed to load data</p>
        <p className="mt-1 max-w-sm text-xs text-[#71717A]">{errorMessage}</p>
        {onRetry && (
          <Button variant="outline" size="sm" onClick={onRetry} className="mt-4">
            <RefreshCw className="h-3.5 w-3.5" />
            Retry
          </Button>
        )}
      </div>
    );
  }

  if (isEmpty) {
    return (
      <div className="flex flex-col items-center justify-center rounded-[10px] border border-[#E4E4E7] bg-white px-6 py-12 text-center">
        <p className="text-sm font-semibold text-[#18181B]">{emptyTitle}</p>
        <p className="mt-1 text-xs text-[#71717A]">{emptyDescription}</p>
      </div>
    );
  }

  return <>{children}</>;
}
