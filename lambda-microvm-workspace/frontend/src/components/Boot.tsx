import { useEffect, useState } from 'react';
import { LogIn } from 'lucide-react';
import { loadRuntimeConfig } from '../lib/config';
import { currentUser, handleRedirectCallback, initAuth, onSessionLost, signIn, type SessionUser } from '../lib/auth';
import { Button } from './ui/primitives';
import { LogoMark, Wordmark } from './ui/Logo';
import { Workspace } from './Workspace';

type Phase =
  | { kind: 'loading' }
  | { kind: 'config-error'; message: string }
  | { kind: 'signed-out'; error: string | null }
  | { kind: 'ready'; user: SessionUser };

export function Boot() {
  const [phase, setPhase] = useState<Phase>({ kind: 'loading' });

  useEffect(() => {
    let off = () => {};
    (async () => {
      let cfg;
      try {
        cfg = await loadRuntimeConfig();
      } catch (e) {
        setPhase({ kind: 'config-error', message: e instanceof Error ? e.message : String(e) });
        return;
      }
      initAuth(cfg);
      const callbackError = await handleRedirectCallback();
      const user = await currentUser().catch(() => null);
      if (!user) {
        setPhase({ kind: 'signed-out', error: callbackError });
        return;
      }
      setPhase({ kind: 'ready', user });
      off = onSessionLost(() => setPhase({ kind: 'signed-out', error: 'Your session ended. Sign in again to continue.' }));
    })();
    return () => off();
  }, []);

  if (phase.kind === 'ready') return <Workspace />;
  if (phase.kind === 'loading') return <div className="gate" aria-busy="true" />;
  return (
    <div className="gate">
      <div className="gate-card">
        <div className="gate-mark">
          <LogoMark size={30} />
          <Wordmark className="gate-wordmark" />
        </div>
        <h1 className="gate-title">Lambda MicroVM Workspace</h1>
        {phase.kind === 'config-error' ? (
          <>
            <p className="gate-text">The workspace can’t start because its configuration is missing.</p>
            <p className="gate-error" role="alert">
              {phase.message}
            </p>
          </>
        ) : (
          <>
            <p className="gate-text">Sign in to manage your sandboxes and open their terminals.</p>
            {phase.error ? (
              <p className="gate-error" role="alert">
                {phase.error}
              </p>
            ) : null}
            <Button variant="primary" icon={<LogIn size={14} aria-hidden />} onClick={() => void signIn()} autoFocus>
              Sign in
            </Button>
          </>
        )}
      </div>
    </div>
  );
}
