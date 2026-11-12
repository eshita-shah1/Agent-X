import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { ApiError, loadToken, login, saveToken } from '@/lib/api';

export function errorText(error: unknown): string {
  if (error instanceof ApiError) return `${error.status}: ${error.detail}`;
  return error instanceof Error ? error.message : String(error);
}

// The access token for this browser tab (sessionStorage), shared by every panel that talks to the backend.
export function useSessionToken(): [string | null, (token: string | null) => void] {
  const [token, setToken] = useState<string | null>(() => loadToken());
  const set = (value: string | null) => {
    saveToken(value);
    setToken(value);
  };
  return [token, set];
}

export function SignInForm({ onToken, idPrefix = 'session' }: { onToken: (token: string) => void; idPrefix?: string }) {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onToken(await login(email, password));
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} className="mt-3 grid gap-3 sm:grid-cols-[1fr_1fr_auto] sm:items-end">
      <div className="space-y-1">
        <Label htmlFor={`${idPrefix}-email`}>Email</Label>
        <Input id={`${idPrefix}-email`} type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
      </div>
      <div className="space-y-1">
        <Label htmlFor={`${idPrefix}-password`}>Password</Label>
        <Input
          id={`${idPrefix}-password`}
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
        />
      </div>
      <Button type="submit" disabled={busy}>
        {busy ? 'Signing in…' : 'Sign in'}
      </Button>
      {error && <p className="text-[12px] text-[#DC2626] sm:col-span-3">{error}</p>}
    </form>
  );
}
