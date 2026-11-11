import { useState, type ReactNode } from 'react';
import { useLocation } from 'react-router-dom';
import { SlidersHorizontal, X } from 'lucide-react';
import { Sidebar } from '@/components/layout/Sidebar';
import { InstrumentsPanel } from '@/components/layout/InstrumentsPanel';
import { ToastViewport } from '@/components/shared/ToastViewport';

interface AppShellProps {
  children: ReactNode;
}

const ROUTES_WITH_INSTRUMENTS_PANEL = new Set(['/dashboard', '/backtesting']);

export function AppShell({ children }: AppShellProps) {
  const location = useLocation();
  const showInstrumentsPanel = ROUTES_WITH_INSTRUMENTS_PANEL.has(location.pathname);
  const [mobileInstrumentsOpen, setMobileInstrumentsOpen] = useState(false);

  return (
    <div className="flex h-screen w-full overflow-hidden bg-[#F9FAFB]">
      {/* Left Navigation Sidebar */}
      <Sidebar />

      {/* Central Workspace — automatically expands to full width when Instruments panel is absent */}
      <main className="relative flex-1 min-w-0 h-full overflow-y-auto overflow-x-hidden">
        {showInstrumentsPanel && (
          <div className="sticky top-3 z-20 flex justify-end px-5 pt-3 lg:hidden">
            <button
              type="button"
              onClick={() => setMobileInstrumentsOpen(true)}
              className="inline-flex items-center gap-1.5 rounded-lg border border-[#E4E4E7] bg-white px-3 py-1.5 text-xs font-medium text-[#18181B] shadow-xs hover:bg-[#F9FAFB]"
            >
              <SlidersHorizontal className="h-3.5 w-3.5 text-[#52647A]" />
              Instruments
            </button>
          </div>
        )}

        <div className="mx-auto w-full px-5 py-5 sm:px-6 lg:px-7">
          {children}
        </div>
      </main>

      {/* Right Instruments Panel — strictly ONLY on /dashboard and /backtesting */}
      {showInstrumentsPanel && (
        <div className="hidden h-full shrink-0 lg:block">
          <InstrumentsPanel />
        </div>
      )}

      {/* Responsive Instruments drawer on smaller viewports ONLY on /dashboard and /backtesting */}
      {showInstrumentsPanel && mobileInstrumentsOpen && (
        <div className="fixed inset-0 z-50 flex justify-end lg:hidden">
          <div
            className="fixed inset-0 bg-zinc-950/35"
            onClick={() => setMobileInstrumentsOpen(false)}
          />
          <div className="relative z-10 h-full bg-white shadow-xl">
            <button
              type="button"
              onClick={() => setMobileInstrumentsOpen(false)}
              className="absolute right-3 top-4 z-20 rounded-md p-1 text-[#71717A] hover:bg-[#F4F4F5]"
              aria-label="Close instruments panel"
            >
              <X className="h-4 w-4" />
            </button>
            <InstrumentsPanel onSelectInstrument={() => setMobileInstrumentsOpen(false)} />
          </div>
        </div>
      )}

      <ToastViewport />
    </div>
  );
}
