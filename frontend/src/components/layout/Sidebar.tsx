import { NavLink, useNavigate } from 'react-router-dom';
import {
  BarChart3,
  Briefcase,
  CandlestickChart,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  FilePenLine,
  FlaskConical,
  LayoutGrid,
  Lightbulb,
  Settings,
  ShieldCheck,
  X,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { useAppStore } from '@/store/useAppStore';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';

const NAV_ITEMS = [
  { label: 'Dashboard', to: '/dashboard', icon: LayoutGrid },
  { label: 'Trading', to: '/trading', icon: CandlestickChart },
  { label: 'Portfolio', to: '/portfolio', icon: Briefcase },
  { label: 'Trade Journal', to: '/journal', icon: FilePenLine },
  { label: 'Daily Insights', to: '/insights', icon: Lightbulb },
  { label: 'Backtesting Lab', to: '/backtesting', icon: FlaskConical },
  { label: 'Risk Management', to: '/risk', icon: ShieldCheck },
  { label: 'Benchmark', to: '/benchmark', icon: BarChart3 },
  { label: 'Settings', to: '/settings', icon: Settings },
] as const;

export function Sidebar() {
  const navigate = useNavigate();
  const sidebarCollapsed = useAppStore((s) => s.sidebarCollapsed);
  const toggleSidebar = useAppStore((s) => s.toggleSidebar);
  const mobileNavOpen = useAppStore((s) => s.mobileNavOpen);
  const setMobileNavOpen = useAppStore((s) => s.setMobileNavOpen);
  const profile = useAppStore((s) => s.settings.profile);
  const agentStatus = useAppStore((s) => s.agentStatus);

  const sidebarContent = (isMobile = false) => {
    const collapsed = !isMobile && sidebarCollapsed;

    return (
      <div
        className={cn(
          'flex h-full flex-col border-r border-[#E4E4E7] bg-white transition-all duration-200',
          collapsed ? 'w-[68px]' : 'w-[224px]'
        )}
      >
        {/* Top brand header */}
        <div
          className={cn(
            'flex items-center justify-between px-4 pt-5 pb-4',
            collapsed && 'justify-center px-2'
          )}
        >
          {!collapsed && (
            <button
              type="button"
              onClick={() => navigate('/dashboard')}
              className="text-left focus:outline-none"
            >
              <div className="flex items-center gap-1.5">
                <span className="text-[20px] font-semibold leading-none tracking-tight text-[#18181B]">
                  AgentX
                </span>
                {agentStatus === 'running' && (
                  <span
                    title="Agent running"
                    className="h-2 w-2 rounded-full bg-emerald-500"
                  />
                )}
              </div>
              <p className="mt-1 text-[9.5px] font-medium uppercase tracking-[0.14em] text-[#71717A]">
                PAPER TRADING
              </p>
            </button>
          )}

          {isMobile ? (
            <button
              type="button"
              onClick={() => setMobileNavOpen(false)}
              className="rounded-md p-1.5 text-[#71717A] hover:bg-[#F4F4F5] hover:text-[#18181B]"
              aria-label="Close navigation"
            >
              <X className="h-4 w-4" />
            </button>
          ) : (
            <button
              type="button"
              onClick={toggleSidebar}
              className="rounded-md p-1.5 text-[#71717A] transition-colors hover:bg-[#F4F4F5] hover:text-[#18181B]"
              aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
              title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
            >
              {collapsed ? (
                <ChevronRight className="h-4 w-4" />
              ) : (
                <ChevronLeft className="h-4 w-4" />
              )}
            </button>
          )}
        </div>

        {/* Workspace Section */}
        <div className="flex-1 overflow-y-auto px-2.5 py-2">
          {!collapsed && (
            <p className="mb-2 px-2.5 text-[10px] font-medium uppercase tracking-[0.09em] text-[#71717A]">
              WORKSPACE
            </p>
          )}

          <nav className="space-y-1" aria-label="Main navigation">
            {NAV_ITEMS.map(({ label, to, icon: Icon }) => (
              <NavLink
                key={to}
                to={to}
                onClick={() => {
                  if (isMobile) setMobileNavOpen(false);
                }}
                title={collapsed ? label : undefined}
                className={({ isActive }) =>
                  cn(
                    'group relative flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-[13px] transition-colors',
                    collapsed && 'justify-center px-2',
                    isActive
                      ? 'bg-[#F1F5F9] font-medium text-[#18181B]'
                      : 'font-normal text-[#52525B] hover:bg-[#F9FAFB] hover:text-[#18181B]'
                  )
                }
              >
                {({ isActive }) => (
                  <>
                    {isActive && !collapsed && (
                      <span className="absolute left-0 top-1/2 h-4 w-[3px] -translate-y-1/2 rounded-r-full bg-[#52647A]" />
                    )}
                    <Icon
                      strokeWidth={1.75}
                      className={cn(
                        'h-4 w-4 shrink-0',
                        isActive ? 'text-[#18181B]' : 'text-[#71717A] group-hover:text-[#18181B]'
                      )}
                    />
                    {!collapsed && <span className="truncate">{label}</span>}
                  </>
                )}
              </NavLink>
            ))}
          </nav>
        </div>

        {/* Bottom Practice Card + Profile */}
        <div className="mt-auto space-y-2.5 p-2.5">
          {!collapsed && (
            <div className="rounded-[10px] border border-[#E4E4E7] bg-[#F9FAFB] p-3">
              <ShieldCheck strokeWidth={1.75} className="mb-1.5 h-4 w-4 text-[#71717A]" />
              <p className="text-[12px] font-semibold text-[#18181B]">Practice. Not risk.</p>
              <p className="mt-0.5 text-[11px] leading-relaxed text-[#71717A]">
                Virtual capital. Real market decisions.
              </p>
            </div>
          )}

          <div className="border-t border-[#E4E4E7] pt-2.5">
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <button
                  type="button"
                  className={cn(
                    'flex w-full items-center justify-between rounded-lg px-2 py-1.5 text-left transition-colors hover:bg-[#F4F4F5]',
                    collapsed && 'justify-center'
                  )}
                >
                  {collapsed ? (
                    <div className="flex h-7 w-7 items-center justify-center rounded-full bg-[#F1F5F9] text-[11px] font-semibold text-[#18181B]">
                      {profile.displayName
                        .split(' ')
                        .map((part) => part[0])
                        .join('')
                        .slice(0, 2)
                        .toUpperCase()}
                    </div>
                  ) : (
                    <>
                      <div className="min-w-0">
                        <p className="truncate text-[11px] text-[#71717A]">
                          {profile.workspace}
                        </p>
                        <p className="truncate text-[13px] font-medium text-[#18181B]">
                          {profile.displayName}
                        </p>
                      </div>
                      <ChevronDown className="h-3.5 w-3.5 shrink-0 text-[#71717A]" />
                    </>
                  )}
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" side="top" className="w-52">
                <DropdownMenuLabel>{profile.email}</DropdownMenuLabel>
                <DropdownMenuSeparator />
                <DropdownMenuItem onClick={() => navigate('/portfolio')}>
                  Paper Portfolio
                </DropdownMenuItem>
                <DropdownMenuItem onClick={() => navigate('/trading')}>
                  Place Paper Order
                </DropdownMenuItem>
                <DropdownMenuItem onClick={() => navigate('/settings')}>
                  Workspace Settings
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </div>
      </div>
    );
  };

  return (
    <>
      {/* Desktop persistent collapsible sidebar */}
      <aside className="hidden h-full shrink-0 md:block">{sidebarContent(false)}</aside>

      {/* Mobile drawer sidebar */}
      {mobileNavOpen && (
        <div className="fixed inset-0 z-50 flex md:hidden">
          <div
            className="fixed inset-0 bg-zinc-950/35"
            onClick={() => setMobileNavOpen(false)}
          />
          <aside className="relative z-10 h-full">{sidebarContent(true)}</aside>
        </div>
      )}
    </>
  );
}
