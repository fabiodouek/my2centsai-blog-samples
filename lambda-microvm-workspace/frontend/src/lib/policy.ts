import { useSyncExternalStore } from 'react';
import { api, type WriteResult } from './api';
import type { ApplyRef, PolicyOverview, PolicyStatus } from './types';

// Shared state for the proxy policy page: the overview (enabled, editable, versions), the sidecar's apply
// status, and the last write this page made. Every write returns `apply` (the config/scope versions the
// sidecar must reach); the status poll runs every second until it reports them, then every 10 s.

interface PolicyState {
  overview: PolicyOverview | null;
  overviewError: string | null;
  status: PolicyStatus | null;
  statusError: string | null;
  /** The last write made from this page, until the sidecar reports it applied. */
  target: ApplyRef | null;
  /** Bumped after every successful write so lists (change log, credentials) refetch. */
  tick: number;
}

let state: PolicyState = { overview: null, overviewError: null, status: null, statusError: null, target: null, tick: 0 };
const listeners = new Set<() => void>();

function set(patch: Partial<PolicyState>) {
  state = { ...state, ...patch };
  listeners.forEach((l) => l());
}

export function usePolicy<T>(selector: (s: PolicyState) => T): T {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => selector(state),
  );
}

export function getPolicyState(): PolicyState {
  return state;
}

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

export async function refreshOverview(): Promise<void> {
  try {
    set({ overview: await api.policy(), overviewError: null });
  } catch (e) {
    set({ overviewError: errText(e) });
  }
}

/** Overview once per page load, for components outside the policy page (the Credentials dock). */
let overviewAsked = false;
export function ensureOverview() {
  if (overviewAsked) return;
  overviewAsked = true;
  void refreshOverview();
}

// ---------- apply status ----------

const FAST_MS = 1000;
const SLOW_MS = 10_000;

/** True while the last change (ours or anyone's, per the backend) hasn't been applied by the sidecar yet. */
export function isPending(st: PolicyStatus | null, target: ApplyRef | null): boolean {
  if (!st) return target !== null;
  if (st.reload.ok === false && !st.pending) return false; // rejected: shown as an error, not a spinner
  if (st.pending) return true;
  if (target) {
    const behind = (want: number | null, have: number | null) => want !== null && (have === null || have < want);
    if (behind(target.configVersion, st.config.version) || behind(target.scopeVersion, st.scope.version)) return true;
  }
  return !st.applied && st.target.configVersion !== null && !st.sidecar.stale && st.reload.ok !== false;
}

let holders = 0;
let timer: number | null = null;
let inflight = false;

async function pollOnce() {
  if (inflight) return;
  inflight = true;
  try {
    const st = await api.policyStatus();
    let target = state.target;
    if (target && !isPending(st, target)) target = null;
    set({ status: st, statusError: null, target });
  } catch (e) {
    set({ statusError: errText(e) });
  } finally {
    inflight = false;
  }
}

function schedule(delay?: number) {
  if (timer !== null) window.clearTimeout(timer);
  timer = null;
  if (holders === 0) return;
  const wait = delay ?? (isPending(state.status, state.target) ? FAST_MS : SLOW_MS);
  timer = window.setTimeout(async () => {
    timer = null;
    if (!document.hidden || isPending(state.status, state.target)) await pollOnce();
    schedule();
  }, wait);
}

/** Polls the apply status while a policy view is mounted. Returns the release function. */
export function holdStatus(): () => void {
  holders++;
  if (holders === 1) {
    void pollOnce().then(() => schedule());
  }
  let released = false;
  return () => {
    if (released) return;
    released = true;
    holders--;
    if (holders === 0 && timer !== null) {
      window.clearTimeout(timer);
      timer = null;
    }
  };
}

/** After a successful write: track its apply, refresh the lists and the overview, and poll fast. */
export function noteWrite(res: WriteResult | null) {
  const apply = res?.apply ?? null;
  set({ target: apply ?? state.target, tick: state.tick + 1 });
  void refreshOverview();
  if (holders > 0) {
    void pollOnce().then(() => schedule());
  }
}
