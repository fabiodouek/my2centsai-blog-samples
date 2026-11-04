import { useEffect, useSyncExternalStore } from 'react';
import { api } from './api';
import type { AwsAction, AwsCatalog, AwsServiceDetail, AwsWildcard } from './types';

// Session cache for the AWS access tab, per role. Whether an action can be picked depends on the role it's evaluated
// for (`?role=` on every catalog route), so the catalog, each service's actions (loaded on demand) and the index of
// actions seen so far are all keyed by role. The backend caches its sources for an hour and sends max-age=3600 too.
// Every action seen in a service or a search result is indexed, so the selection chips can say whether an action is
// still allowed for the selected role even when its service isn't open.

export interface KnownAction {
  allowed: boolean;
  limited: boolean;
  reason: string | null;
}

interface Entry<T> {
  data: T | null;
  error: string | null;
  promise: Promise<T> | null;
}

/** '' is "the server's default role" (before the scope says which role is applied). */
const roleKey = (role: string | null | undefined) => role ?? '';

const catalogs = new Map<string, Entry<AwsCatalog>>();
const details = new Map<string, Entry<AwsServiceDetail>>();
const known = new Map<string, KnownAction>();
const listeners = new Set<() => void>();
let version = 0;
let lastCatalog: AwsCatalog | null = null;

function emit() {
  version += 1;
  listeners.forEach((l) => l());
}

function subscribe(l: () => void) {
  listeners.add(l);
  return () => listeners.delete(l);
}

export function useCatalogVersion(): number {
  return useSyncExternalStore(subscribe, () => version);
}

export function remember(items: (AwsAction | AwsWildcard)[], role: string | null) {
  let changed = false;
  for (const a of items) {
    const k = `${roleKey(role)}|${a.action}`;
    const limited = 'limited' in a ? a.limited : false;
    const prev = known.get(k);
    if (!prev || prev.allowed !== a.allowed || prev.reason !== a.reason || prev.limited !== limited) {
      known.set(k, { allowed: a.allowed, limited, reason: a.reason });
      changed = true;
    }
  }
  if (changed) emit();
}

export function knownAction(action: string, role: string | null): KnownAction | null {
  return known.get(`${roleKey(role)}|${action}`) ?? null;
}

// ---------------------------------------------------------------- the catalog (service list) per role

function loadCatalog(role: string | null): Promise<AwsCatalog> {
  const k = roleKey(role);
  const e = catalogs.get(k);
  if (e?.data) return Promise.resolve(e.data);
  if (e?.promise) return e.promise;
  const promise = api.awsCatalog(role).then(
    (d) => {
      catalogs.set(k, { data: d, error: null, promise: null });
      // The server's default answer is also that role's answer
      if (!role && d.role && !catalogs.get(d.role)?.data) catalogs.set(d.role, { data: d, error: null, promise: null });
      lastCatalog = d;
      emit();
      return d;
    },
    (err: unknown) => {
      catalogs.set(k, { data: null, error: err instanceof Error ? err.message : String(err), promise: null });
      emit();
      throw err;
    },
  );
  catalogs.set(k, { data: null, error: null, promise });
  emit();
  return promise;
}

/**
 * The catalog evaluated for `role`. While a newly picked role loads, `data` is the last catalog seen (any role) and
 * `stale` is true, so the page doesn't blank out; `current` is only that role's.
 */
export function useRoleCatalog(role: string | null): {
  data: AwsCatalog | null;
  current: AwsCatalog | null;
  error: string | null;
  loading: boolean;
  stale: boolean;
} {
  useCatalogVersion();
  useEffect(() => {
    void loadCatalog(role).catch(() => undefined);
  }, [role]);
  const e = catalogs.get(roleKey(role));
  const current = e?.data ?? null;
  return { data: current ?? lastCatalog, current, error: e?.error ?? null, loading: !current, stale: !current && lastCatalog !== null };
}

export function retryCatalog(role: string | null) {
  catalogs.delete(roleKey(role));
  void loadCatalog(role).catch(() => undefined);
}

// ---------------------------------------------------------------- a service's actions per role

export function loadDetail(id: string, role: string | null): Promise<AwsServiceDetail> {
  const k = `${roleKey(role)}|${id}`;
  const e = details.get(k);
  if (e?.data) return Promise.resolve(e.data);
  if (e?.promise) return e.promise;
  const promise = api.awsService(id, role).then(
    (d) => {
      details.set(k, { data: d, error: null, promise: null });
      remember([...d.actions, ...d.wildcards], role);
      emit();
      return d;
    },
    (err: unknown) => {
      details.set(k, { data: null, error: err instanceof Error ? err.message : String(err), promise: null });
      emit();
      throw err;
    },
  );
  details.set(k, { data: null, error: null, promise });
  emit();
  return promise;
}

/** Load (once per role) every service named by these actions, so their chips can be checked. */
export function ensureDetails(actions: string[], role: string | null) {
  for (const id of new Set(actions.map((a) => a.split(':')[0]).filter(Boolean))) {
    if (!details.has(`${roleKey(role)}|${id}`)) void loadDetail(id, role).catch(() => undefined);
  }
}

export function useServiceDetail(
  id: string | null,
  role: string | null,
): { data: AwsServiceDetail | null; error: string | null; loading: boolean } {
  useCatalogVersion();
  useEffect(() => {
    if (id) void loadDetail(id, role).catch(() => undefined);
  }, [id, role]);
  if (!id) return { data: null, error: null, loading: false };
  const e = details.get(`${roleKey(role)}|${id}`);
  return { data: e?.data ?? null, error: e?.error ?? null, loading: !e || Boolean(e.promise) };
}

export function retryDetail(id: string, role: string | null) {
  details.delete(`${roleKey(role)}|${id}`);
  void loadDetail(id, role).catch(() => undefined);
}
