import { useSyncExternalStore } from 'react';
import { api, ApiError } from './api';
import { hub } from './events';
import { readPref, writePref } from './hooks';
import type { AppConfig, Me, NewSandbox, Quota, Sandbox, SandboxState } from './types';

// App-wide workspace state: sandboxes, quota, selection, which terminals this browser has
// attached, and toasts. A tiny external store so any component can read a slice.

export interface Toast {
  id: number;
  kind: 'error' | 'info' | 'success';
  title: string;
  body?: string;
}

export type PendingAction = 'suspend' | 'resume' | 'terminate';

/** What the main area shows: the selected sandbox's terminal, or the global proxy policy page. */
export type View = 'terminal' | 'policy';
export type PolicySection = 'allowlist' | 'credentials' | 'aws' | 'changes' | 'config';
/** `config` (the proxy's config as it is in the bucket against what it applied) is for admins only. */
export const POLICY_SECTIONS: PolicySection[] = ['allowlist', 'credentials', 'aws', 'changes', 'config'];

interface State {
  me: Me | null;
  config: AppConfig | null;
  sandboxes: Sandbox[] | null;
  sandboxesError: string | null;
  quota: Quota | null;
  selectedId: string | null;
  /** The app picked the selection itself (the chosen sandbox went away), so its terminal waits for an Attach click. */
  autoSelected: boolean;
  attached: Record<string, boolean>;
  /** A terminal in this browser is attached, connecting or reconnecting (so the sandbox isn't idling on our account). */
  linked: Record<string, boolean>;
  /** Epoch ms of the last moment this browser knows the sandbox saw endpoint traffic. */
  activityAt: Record<string, number>;
  pending: Record<string, PendingAction | undefined>;
  toasts: Toast[];
  /** The New sandbox form is open in the sidebar. */
  creating: boolean;
  /** Mirrors the URL hash: #policy/<section> opens the policy page. */
  view: View;
  policySection: PolicySection;
}

const ACTIVITY_KEY = 'ws.activity.v1';
const SELECTED_KEY = 'ws.selected.v1';

let state: State = {
  me: null,
  config: null,
  sandboxes: null,
  sandboxesError: null,
  quota: null,
  selectedId: readPref<string | null>(SELECTED_KEY, null),
  autoSelected: false,
  attached: {},
  linked: {},
  activityAt: readPref<Record<string, number>>(ACTIVITY_KEY, {}),
  pending: {},
  toasts: [],
  creating: false,
  ...routeFromHash(),
};

const listeners = new Set<() => void>();

function set(patch: Partial<State> | ((s: State) => Partial<State>)) {
  const p = typeof patch === 'function' ? patch(state) : patch;
  state = { ...state, ...p };
  listeners.forEach((l) => l());
}

export function useStore<T>(selector: (s: State) => T): T {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => selector(state),
  );
}

export function getState(): State {
  return state;
}

// ---------- toasts ----------

let toastSeq = 0;
export function toast(kind: Toast['kind'], title: string, body?: string, ttlMs = kind === 'error' ? 9000 : 4500) {
  const id = ++toastSeq;
  set((s) => ({ toasts: [...s.toasts.slice(-3), { id, kind, title, body }] }));
  window.setTimeout(() => dismissToast(id), ttlMs);
}

export function dismissToast(id: number) {
  set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) }));
}

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

// ---------- activity (for the idle countdown estimate) ----------

function markActivity(id: string, at = Date.now()) {
  const activityAt = { ...state.activityAt, [id]: at };
  set({ activityAt });
  writePref(ACTIVITY_KEY, activityAt);
}

/**
 * Best estimate of when idle suspend fires. The backend does not report last endpoint traffic
 * (unless it sends last_activity_at), so this counts from the latest of: that field, the last
 * time this browser detached or saw the sandbox start/resume, or when this page first saw it.
 */
export function idleDeadline(sb: Sandbox, s: State = state): number | null {
  if (sb.state !== 'RUNNING' || s.linked[sb.id] || sb.attachedAnywhere) return null;
  const base = Math.max(sb.lastActivityAt ?? 0, s.activityAt[sb.id] ?? 0, sb.startedAt ?? 0);
  return base + sb.idleMinutes * 60_000;
}

// ---------- loading ----------

export async function loadInitial() {
  const results = await Promise.allSettled([api.me(), api.config(), refreshSandboxes(), refreshQuota()]);
  const [me, config] = results;
  set({
    me: me.status === 'fulfilled' ? me.value : null,
    config: config.status === 'fulfilled' ? config.value : null,
  });
}

// ---------- transition guards ----------
// After an action (or when the terminal proves the VM's state), a list poll can still return the
// control plane's previous state for a moment. For a few seconds, a polled state equal to the state
// we just left is ignored. Lifecycle events from /ws/events always win.
const GUARD_MS = 6000;
const guards = new Map<string, { from: SandboxState; until: number }>();

function guard(id: string, from: SandboxState | undefined) {
  if (from) guards.set(id, { from, until: Date.now() + GUARD_MS });
}

function guarded(id: string, polled: SandboxState): boolean {
  const g = guards.get(id);
  if (!g) return false;
  if (Date.now() > g.until) {
    guards.delete(id);
    return false;
  }
  return polled === g.from;
}

const TRANSITIONAL: SandboxState[] = ['PENDING', 'SUSPENDING', 'RESUMING', 'TERMINATING'];
export function isTransitionalState(st: SandboxState) {
  return TRANSITIONAL.includes(st);
}

/** The terminal socket saw the VM's real state (attached => RUNNING, suspended => SUSPENDED). */
export function observeState(id: string, observed: SandboxState) {
  const sb = state.sandboxes?.find((s) => s.id === id);
  if (!sb || sb.state === observed || sb.state === 'TERMINATED' || sb.state === 'TERMINATING') return;
  guard(id, sb.state);
  patchSandbox(id, { state: observed });
}

export async function refreshSandboxes() {
  try {
    const fetched = await api.sandboxes();
    const current = new Map((state.sandboxes ?? []).map((s) => [s.id, s]));
    const list = fetched.map((sb) => {
      const cur = current.get(sb.id);
      return cur && guarded(sb.id, sb.state) ? { ...sb, state: cur.state } : sb;
    });
    const known = new Set((state.sandboxes ?? []).map((s) => s.id));
    const activityAt = { ...state.activityAt };
    let changed = false;
    for (const sb of list) {
      // First sighting of a running sandbox with no history at all: start the idle estimate now.
      if (sb.state === 'RUNNING' && !known.has(sb.id) && !activityAt[sb.id] && !sb.lastActivityAt) {
        activityAt[sb.id] = Date.now();
        changed = true;
      }
    }
    if (changed) writePref(ACTIVITY_KEY, activityAt);
    let selectedId = state.selectedId;
    let autoSelected = state.autoSelected;
    const alive = list.filter((s) => s.state !== 'TERMINATED');
    if (!selectedId || !list.some((s) => s.id === selectedId)) {
      // Show something, but don't open a terminal the user didn't pick: attaching wakes the sandbox and keeps it awake.
      selectedId = alive[0]?.id ?? list[0]?.id ?? null;
      autoSelected = true;
    }
    set({ sandboxes: list, sandboxesError: null, activityAt, selectedId, autoSelected });
  } catch (e) {
    set({ sandboxesError: errText(e) });
  }
}

export async function refreshQuota() {
  try {
    set({ quota: await api.quota() });
  } catch {
    /* quota is advisory; keep the last value */
  }
}

/** Coalesces bursts (several lifecycle events at once) into one /api/quota call. */
let quotaTimer: number | null = null;
function scheduleQuotaRefresh(delayMs = 250) {
  if (quotaTimer !== null) window.clearTimeout(quotaTimer);
  quotaTimer = window.setTimeout(() => {
    quotaTimer = null;
    void refreshQuota();
  }, delayMs);
}

export function setCreating(creating: boolean) {
  set({ creating });
}

// ---------- selection & attachment ----------

export function select(id: string) {
  set({ selectedId: id, autoSelected: false });
  writePref(SELECTED_KEY, id);
  // Picking a sandbox always goes back to its terminal.
  if (state.view === 'policy') closePolicy();
}

// ---------- view routing (#policy/<section>) ----------

function routeFromHash(): { view: View; policySection: PolicySection } {
  const m = window.location.hash.match(/^#policy(?:\/([a-z]+))?$/);
  if (!m) return { view: 'terminal', policySection: readPref<PolicySection>('ws.policySection.v1', 'allowlist') };
  const sec = (POLICY_SECTIONS as string[]).includes(m[1] ?? '') ? (m[1] as PolicySection) : 'allowlist';
  return { view: 'policy', policySection: sec };
}

function writeHash(hash: string, push: boolean) {
  const url = `${window.location.pathname}${window.location.search}${hash}`;
  if (`${window.location.pathname}${window.location.search}${window.location.hash}` === url) return;
  if (push) window.history.pushState(null, '', url);
  else window.history.replaceState(null, '', url);
}

export function openPolicy(section: PolicySection = state.policySection) {
  const push = state.view !== 'policy';
  set({ view: 'policy', policySection: section, creating: false });
  writePref('ws.policySection.v1', section);
  writeHash(`#policy/${section}`, push);
}

export function setPolicySection(section: PolicySection) {
  set({ policySection: section });
  writePref('ws.policySection.v1', section);
  if (state.view === 'policy') writeHash(`#policy/${section}`, false);
}

export function closePolicy() {
  set({ view: 'terminal' });
  writeHash('', false);
}

let routeWired = false;
/** Back/forward and hand-edited hashes switch the view too. */
export function wireRoute() {
  if (routeWired) return;
  routeWired = true;
  const sync = () => {
    const r = routeFromHash();
    if (r.view !== state.view || (r.view === 'policy' && r.policySection !== state.policySection)) {
      set(r.view === 'policy' ? r : { view: 'terminal' });
    }
  };
  window.addEventListener('hashchange', sync);
  window.addEventListener('popstate', sync);
}

/** `attached`: live polling may run. `linked`: a terminal socket is attached or (re)connecting. */
export function setTerminalLink(id: string, attached: boolean, linked: boolean) {
  const wasLinked = Boolean(state.linked[id]);
  if (Boolean(state.attached[id]) === attached && wasLinked === linked) return;
  set((s) => ({
    attached: { ...s.attached, [id]: attached },
    linked: { ...s.linked, [id]: linked },
    // Assume ours was the only terminal until the next list refresh says otherwise.
    sandboxes: s.sandboxes?.map((sb) => (sb.id === id ? { ...sb, attachedAnywhere: linked } : sb)) ?? null,
  }));
  // Leaving a terminal is the last endpoint traffic this browser knows about.
  if (wasLinked && !linked) markActivity(id);
}

function patchSandbox(id: string, patch: Partial<Sandbox>) {
  set((s) => ({ sandboxes: s.sandboxes?.map((sb) => (sb.id === id ? { ...sb, ...patch } : sb)) ?? null }));
}

// ---------- actions ----------

export async function createSandbox(input: NewSandbox): Promise<boolean> {
  try {
    const sb = await api.createSandbox(input);
    set((s) => ({ sandboxes: [...(s.sandboxes ?? []).filter((x) => x.id !== sb.id), sb] }));
    select(sb.id);
    markActivity(sb.id);
    void refreshQuota();
    window.setTimeout(() => void refreshSandboxes(), 2500);
    return true;
  } catch (e) {
    // AWS's ServiceQuotaExceededException (the backend never refuses on its own estimate): the detail quotes AWS,
    // names the size and gives the estimate; the bar is refreshed so it shows what's live now
    if (e instanceof ApiError && e.status === 409 && /ServiceQuotaExceeded/.test(errText(e))) {
      toast('error', 'AWS refused it: MicroVM memory quota', errText(e));
      void refreshQuota();
    } else toast('error', 'Could not create the sandbox', errText(e));
    return false;
  }
}

const optimistic: Record<PendingAction, SandboxState> = {
  suspend: 'SUSPENDING',
  resume: 'RESUMING',
  terminate: 'TERMINATING',
};

export async function runAction(id: string, action: PendingAction): Promise<boolean> {
  const sb = state.sandboxes?.find((s) => s.id === id);
  set((s) => ({ pending: { ...s.pending, [id]: action } }));
  const prevState = sb?.state;
  patchSandbox(id, { state: optimistic[action] });
  guard(id, prevState);
  try {
    const item =
      action === 'suspend' ? await api.suspend(id) : action === 'resume' ? await api.resume(id) : await api.terminate(id);
    // Apply the state the API reports right after the call, unless it is still the old one.
    if (item && item.id === id && item.state !== prevState && item.state !== 'UNKNOWN') {
      patchSandbox(id, { state: item.state, stateReason: item.stateReason });
    }
    if (action === 'resume') markActivity(id);
    void refreshQuota();
    // TERMINATING -> TERMINATED takes about 2 s; check again then in case the lifecycle event is missed.
    if (action === 'terminate') window.setTimeout(() => void refreshQuota(), 3000);
    void refreshSandboxes();
    return true;
  } catch (e) {
    guards.delete(id);
    if (prevState) patchSandbox(id, { state: prevState });
    const verb = action === 'terminate' ? 'terminate' : action;
    toast('error', `Could not ${verb} ${sb?.name ?? 'the sandbox'}`, errText(e));
    return false;
  } finally {
    set((s) => ({ pending: { ...s.pending, [id]: undefined } }));
  }
}

// ---------- live lifecycle events ----------

let wired = false;
export function wireLifecycle() {
  if (wired) return;
  wired = true;
  hub.onLifecycle((e) => {
    const known = state.sandboxes?.some((s) => s.id === e.id);
    if (!known) {
      void refreshSandboxes();
      scheduleQuotaRefresh();
      return;
    }
    // A connect-time snapshot can lag behind what we just did; a real transition always applies.
    if (!e.prev && guarded(e.id, e.state)) return;
    guards.delete(e.id);
    patchSandbox(e.id, { state: e.state });
    // Initial snapshots (prev null) are current state, not a resume: they say nothing about activity.
    if (e.state === 'RUNNING' && e.prev) markActivity(e.id, e.ts);
    // Any real transition can change the meter (a terminate frees a slot, a create takes one): refresh at once
    // instead of waiting for the 20 s poll.
    if (e.prev) scheduleQuotaRefresh();
  });
}
