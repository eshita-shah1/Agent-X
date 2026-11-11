import { AlertTriangle, CheckCircle2, Info, X, XCircle } from 'lucide-react';
import { useAppStore } from '@/store/useAppStore';
import { cn } from '@/lib/utils';

export function ToastViewport() {
  const toasts = useAppStore((s) => s.toasts);
  const dismissToast = useAppStore((s) => s.dismissToast);

  if (toasts.length === 0) return null;

  return (
    <div
      className="pointer-events-none fixed bottom-4 right-4 z-[60] flex w-full max-w-sm flex-col gap-2"
      aria-live="polite"
    >
      {toasts.map((toast) => {
        const Icon =
          toast.variant === 'success'
            ? CheckCircle2
            : toast.variant === 'destructive'
              ? XCircle
              : toast.variant === 'warning'
                ? AlertTriangle
                : Info;

        return (
          <div
            key={toast.id}
            className={cn(
              'pointer-events-auto flex items-start gap-2.5 rounded-xl border bg-white p-3.5 shadow-lg',
              toast.variant === 'destructive'
                ? 'border-red-200'
                : toast.variant === 'warning'
                  ? 'border-amber-200'
                  : toast.variant === 'success'
                    ? 'border-emerald-200'
                    : 'border-[#E4E4E7]'
            )}
          >
            <Icon
              className={cn(
                'mt-0.5 h-4 w-4 shrink-0',
                toast.variant === 'destructive'
                  ? 'text-[#DC2626]'
                  : toast.variant === 'warning'
                    ? 'text-[#D97706]'
                    : toast.variant === 'success'
                      ? 'text-[#16A34A]'
                      : 'text-[#52647A]'
              )}
            />
            <div className="min-w-0 flex-1">
              <p className="text-[13px] font-medium text-[#18181B]">{toast.title}</p>
              {toast.description && (
                <p className="mt-0.5 text-[11.5px] leading-snug text-[#71717A]">
                  {toast.description}
                </p>
              )}
            </div>
            <button
              type="button"
              onClick={() => dismissToast(toast.id)}
              className="rounded p-0.5 text-[#71717A] hover:bg-[#F4F4F5] hover:text-[#18181B]"
              aria-label="Dismiss notification"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        );
      })}
    </div>
  );
}
