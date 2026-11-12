import { useState } from 'react';
import { Pause, Play, RotateCcw, Square, OctagonX, LogOut } from 'lucide-react';
import { Card } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { ApiError } from '@/lib/api';
import { errorText, SignInForm, useSessionToken } from '@/components/auth/SignInForm';
import {
  useAgentActionMutation,
  useAgentDecisionsQuery,
  useAgentKillSwitchMutation,
  useAgentStatusQuery,
} from '@/hooks/useAgentQueries';
import type { AgentAction, AgentStatus, AgentStatusResponse } from '@/types/agent';

const STATUS_VARIANT: Record<AgentStatus, 'positive' | 'warning' | 'negative' | 'default'> = {
  RUNNING: 'positive',
  PAUSED: 'warning',
  ERROR: 'negative',
  STOPPED: 'default',
};

function time(value: string | null): string {
  return value ? new Date(value).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata' }) + ' IST' : 'never';
}

function actionsFor(status: AgentStatus): AgentAction[] {
  if (status === 'STOPPED') return ['start'];
  if (status === 'RUNNING') return ['pause', 'stop'];
  return ['resume', 'stop'];
}

const ACTION_LABEL: Record<AgentAction, { label: string; icon: typeof Play }> = {
  start: { label: 'Start', icon: Play },
  pause: { label: 'Pause', icon: Pause },
  resume: { label: 'Resume', icon: RotateCcw },
  stop: { label: 'Stop', icon: Square },
};

function StatusView({ token, status }: { token: string; status: AgentStatusResponse }) {
  const action = useAgentActionMutation(token);
  const kill = useAgentKillSwitchMutation(token);
  const decisions = useAgentDecisionsQuery(token, status.configured);
  const [killOpen, setKillOpen] = useState(false);
  const [reason, setReason] = useState('');
  const failure = action.error ?? kill.error;

  if (!status.configured || !status.status) {
    return (
      <p className="mt-3 text-[12.5px] text-[#52525B]">
        No agent is configured for this account. Save a configuration with{' '}
        <code className="rounded bg-[#F4F4F5] px-1">PUT /api/v1/agent/config</code> (see docs/PHASE5.md).
      </p>
    );
  }

  return (
    <div className="mt-3 space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-[12px]">
        <Badge variant={STATUS_VARIANT[status.status]}>{status.status}</Badge>
        {status.mode && <Badge variant={status.mode === 'SIGNALS_ONLY' ? 'warning' : 'slate'}>{status.mode}</Badge>}
        <Badge variant={status.entries_allowed ? 'positive' : 'default'}>
          {status.entries_allowed ? 'Entries allowed' : 'Entries blocked'}
        </Badge>
        {status.kill_switch_engaged && <Badge variant="negative">Kill switch engaged</Badge>}
        <span className="text-[#71717A]">
          Last cycle: {time(status.last_heartbeat_at)}
          {status.effective_config && ` · config v${status.effective_config.version}`}
        </span>
      </div>

      <div className="flex flex-wrap gap-2">
        {actionsFor(status.status).map((a) => {
          const { label, icon: Icon } = ACTION_LABEL[a];
          return (
            <Button key={a} variant={a === 'stop' ? 'outline' : 'default'} size="sm" disabled={action.isPending}
                    onClick={() => action.mutate(a)}>
              <Icon className="h-3.5 w-3.5" />
              {label}
            </Button>
          );
        })}
        <Button variant="dangerOutline" size="sm" onClick={() => setKillOpen(true)} disabled={status.kill_switch_engaged}>
          <OctagonX className="h-3.5 w-3.5" />
          Kill switch
        </Button>
      </div>
      {failure && <p className="text-[12px] text-[#DC2626]">{errorText(failure)}</p>}

      {status.blockers.length > 0 && (
        <ul className="list-disc space-y-0.5 pl-5 text-[12px] text-[#B45309]">
          {status.blockers.map((b) => (
            <li key={b}>{b}</li>
          ))}
        </ul>
      )}
      {status.disclosures.map((d) => (
        <p key={d} className="text-[11.5px] leading-relaxed text-[#71717A]">
          {d}
        </p>
      ))}

      <div>
        <h3 className="text-[12.5px] font-semibold text-[#18181B]">Recent decisions</h3>
        {decisions.isError && <p className="text-[12px] text-[#DC2626]">{errorText(decisions.error)}</p>}
        {decisions.data && decisions.data.length === 0 && (
          <p className="text-[12px] text-[#71717A]">No decisions recorded yet.</p>
        )}
        {decisions.data && decisions.data.length > 0 && (
          <div className="mt-1 overflow-x-auto">
            <table className="w-full text-left text-[11.5px]">
              <thead className="text-[#71717A]">
                <tr>
                  <th className="py-1 pr-3 font-medium">Bar (IST)</th>
                  <th className="py-1 pr-3 font-medium">Instrument</th>
                  <th className="py-1 pr-3 font-medium">Outcome</th>
                  <th className="py-1 pr-3 font-medium">Signal</th>
                  <th className="py-1 font-medium">Order</th>
                </tr>
              </thead>
              <tbody className="text-[#18181B]">
                {decisions.data.map((d) => (
                  <tr key={d.id} className="border-t border-[#F4F4F5]">
                    <td className="py-1 pr-3 whitespace-nowrap">{time(d.bar_start)}</td>
                    <td className="py-1 pr-3">{d.instrument_id}</td>
                    <td className="py-1 pr-3">{d.outcome}</td>
                    <td className="py-1 pr-3">{[d.strategy_id, d.reason].filter(Boolean).join(' · ') || '—'}</td>
                    <td className="py-1">{d.order_status ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <Dialog open={killOpen} onOpenChange={setKillOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Engage the kill switch?</DialogTitle>
            <DialogDescription className="pt-1 leading-relaxed">
              Blocks every new entry, cancels all open orders and pauses the agent. Open positions are NOT sold: they
              stay visible and keep their stop-loss/take-profit monitoring. Releasing the switch (Risk page /
              POST /risk/kill-switch) does not restart the agent; resume it explicitly.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-1">
            <Label htmlFor="kill-reason">Reason (recorded)</Label>
            <Input id="kill-reason" value={reason} onChange={(e) => setReason(e.target.value)} />
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setKillOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={!reason.trim() || kill.isPending}
              onClick={() => kill.mutate(reason.trim(), { onSuccess: () => setKillOpen(false) })}
            >
              Engage kill switch
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

export function AgentControlPanel() {
  const [token, setSession] = useSessionToken();
  const status = useAgentStatusQuery(token);
  const unauthorized = status.error instanceof ApiError && status.error.status === 401;

  return (
    <Card className="p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-[15px] font-semibold text-[#18181B]">Agent control</h2>
          <p className="mt-0.5 text-[11.5px] text-[#71717A]">
            Live state from the Agent-X backend. Paper trading only.
          </p>
        </div>
        {token && (
          <Button variant="ghost" size="sm" onClick={() => setSession(null)}>
            <LogOut className="h-3.5 w-3.5" />
            Sign out
          </Button>
        )}
      </div>

      {(!token || unauthorized) && <SignInForm onToken={setSession} idPrefix="agent" />}
      {token && status.isLoading && <p className="mt-3 text-[12px] text-[#71717A]">Loading agent status…</p>}
      {token && status.isError && !unauthorized && (
        <p className="mt-3 text-[12px] text-[#DC2626]">Backend unavailable: {errorText(status.error)}</p>
      )}
      {token && status.data && <StatusView token={token} status={status.data} />}
    </Card>
  );
}
