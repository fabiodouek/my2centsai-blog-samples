import { apiUrl } from './config';
import { getIdToken, signIn } from './auth';
import type {
  Allowlist,
  AllowHost,
  AppConfig,
  ApplyRef,
  AccessLevel,
  AwsAction,
  AwsCatalog,
  AwsPreview,
  AwsRole,
  AwsScope,
  AwsSearchResult,
  AwsServiceDetail,
  AwsServiceSummary,
  CatalogCeiling,
  CatalogSource,
  ClaudeSession,
  ClaudeUsage,
  Credential,
  CredentialStatus,
  IdlePolicy,
  Me,
  NewCredential,
  Pane,
  PolicyChange,
  PolicyCredential,
  PolicyOverview,
  PolicyStatus,
  Quota,
  Sandbox,
  SandboxState,
  Stats,
  Vitals,
} from './types';

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

type Json = Record<string, unknown>;

async function request<T = unknown>(method: string, path: string, body?: unknown, retried = false): Promise<T> {
  const token = await getIdToken(retried);
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  let res: Response;
  try {
    res = await fetch(apiUrl(path), { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    throw new ApiError(0, 'Network error: the workspace API is unreachable.');
  }
  if (res.status === 401) {
    if (!retried) return request<T>(method, path, body, true);
    void signIn();
    throw new ApiError(401, 'Your session expired. Redirecting to sign in.');
  }
  const text = await res.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  if (!res.ok) throw new ApiError(res.status, errorMessage(res.status, data));
  return data as T;
}

function errorMessage(status: number, data: unknown): string {
  if (data && typeof data === 'object') {
    const d = data as Json;
    const detail = d.detail ?? d.message ?? d.error;
    if (typeof detail === 'string') return detail;
    if (detail && typeof detail === 'object' && typeof (detail as Json).message === 'string') {
      return (detail as Json).message as string;
    }
    if (Array.isArray(detail) && detail[0] && typeof (detail[0] as Json).msg === 'string') {
      // FastAPI validation error: name the field (loc ends with it) and drop pydantic's "Value error, " prefix.
      const first = detail[0] as Json;
      const msg = String(first.msg).replace(/^Value error, /, '');
      const loc = Array.isArray(first.loc) ? first.loc.map(String) : [];
      const field = loc.length > 1 && loc[0] === 'body' ? loc[loc.length - 1] : null;
      return field && !msg.toLowerCase().startsWith(field.toLowerCase()) ? `${field}: ${msg}` : msg;
    }
  }
  if (typeof data === 'string' && data.length < 200) return data;
  return `Request failed (${status}).`;
}

// ---------- normalizers (tolerant of snake/camel case and timestamp formats) ----------

function pick(o: Json | undefined | null, ...keys: string[]): unknown {
  if (!o) return undefined;
  for (const k of keys) if (o[k] !== undefined && o[k] !== null) return o[k];
  return undefined;
}

function num(v: unknown, fallback = 0): number {
  const n = typeof v === 'string' ? Number(v) : (v as number);
  return typeof n === 'number' && Number.isFinite(n) ? n : fallback;
}

function numOrNull(v: unknown): number | null {
  if (v === undefined || v === null || v === '') return null;
  const n = num(v, NaN);
  return Number.isNaN(n) ? null : n;
}

/** Epoch seconds, epoch ms, or ISO string → epoch ms. */
export function toMs(v: unknown): number | null {
  if (v === undefined || v === null || v === '') return null;
  if (typeof v === 'number') return v < 1e12 ? v * 1000 : v;
  if (typeof v === 'string') {
    if (/^\d+(\.\d+)?$/.test(v)) return toMs(Number(v));
    const t = Date.parse(v);
    return Number.isNaN(t) ? null : t;
  }
  return null;
}

const STATES: SandboxState[] = ['PENDING', 'RUNNING', 'SUSPENDING', 'SUSPENDED', 'RESUMING', 'TERMINATING', 'TERMINATED'];
export function toState(v: unknown): SandboxState {
  const s = String(v ?? '').toUpperCase();
  return (STATES as string[]).includes(s) ? (s as SandboxState) : 'UNKNOWN';
}

export function normSandbox(raw: Json): Sandbox {
  return {
    id: String(pick(raw, 'id', 'microvm_id', 'microvmId') ?? ''),
    name: String(pick(raw, 'name') ?? pick(raw, 'id') ?? 'sandbox'),
    state: toState(pick(raw, 'state', 'status')),
    createdAt: toMs(pick(raw, 'created_at', 'createdAt')),
    startedAt: toMs(pick(raw, 'started_at', 'startedAt')),
    maxDurationS: num(pick(raw, 'max_duration_s', 'maxDurationS'), 8 * 3600),
    idleMinutes: num(pick(raw, 'idle_minutes', 'idleMinutes'), 15),
    imageVersion: (pick(raw, 'image_version', 'imageVersion') as string | undefined) ?? null,
    expiresAt: toMs(pick(raw, 'expires_at', 'expiresAt')),
    stateReason: (pick(raw, 'state_reason', 'stateReason') as string | undefined) ?? null,
    attachedAnywhere: Boolean(pick(raw, 'attached')),
    lastActivityAt: toMs(pick(raw, 'last_activity_at', 'lastActivityAt')),
    terminatedAt: toMs(pick(raw, 'terminated_at', 'terminatedAt')),
  };
}

function normIdle(raw: unknown): IdlePolicy | null {
  if (!raw || typeof raw !== 'object') return null;
  const o = raw as Json;
  return {
    autoResumeEnabled: Boolean(pick(o, 'auto_resume', 'autoResumeEnabled', 'auto_resume_enabled') ?? true),
    maxIdleDurationS: num(pick(o, 'max_idle_s', 'maxIdleDurationSeconds', 'max_idle_duration_s', 'max_idle_duration_seconds')),
    suspendedDurationS: numOrNull(
      pick(o, 'suspendedDurationSeconds', 'suspended_duration_s', 'suspended_duration_seconds'),
    ),
  };
}

function normStats(raw: unknown): Stats | null {
  if (!raw || typeof raw !== 'object') return null;
  const o = raw as Json;
  if (pick(o, 'cpu_pct', 'cpuPct') === undefined) return null;
  return {
    cpuPct: num(pick(o, 'cpu_pct', 'cpuPct')),
    cpuCount: num(pick(o, 'cpu_count', 'cpuCount'), 1),
    memTotalMb: num(pick(o, 'mem_total_mb', 'memTotalMb')),
    memUsedMb: num(pick(o, 'mem_used_mb', 'memUsedMb')),
    load1: num(pick(o, 'load1')),
    diskUsedMb: num(pick(o, 'disk_used_mb', 'diskUsedMb')),
    diskTotalMb: num(pick(o, 'disk_total_mb', 'diskTotalMb')),
    uptimeS: num(pick(o, 'uptime_s', 'uptimeS')),
    procs: num(pick(o, 'procs')),
  };
}

function normPane(raw: unknown): Pane | null {
  if (!raw || typeof raw !== 'object') return null;
  const o = raw as Json;
  return {
    command: String(pick(o, 'command') ?? ''),
    claudeRunning: Boolean(pick(o, 'claude_running', 'claudeRunning')),
    atShell: Boolean(pick(o, 'at_shell', 'atShell')),
  };
}

export function normVitals(raw: Json): Vitals {
  const startedAt = toMs(pick(raw, 'started_at', 'startedAt'));
  const maxDurationS = num(pick(raw, 'max_duration_s', 'maxDurationS'), 8 * 3600);
  const statsRaw = pick(raw, 'stats', 'live', 'agent');
  const statsErr = pick(raw, 'stats_error', 'live_error', 'agent_error');
  return {
    state: toState(pick(raw, 'state', 'status')),
    startedAt,
    maxDurationS,
    expiresAt: toMs(pick(raw, 'expires_at', 'expiresAt')) ?? (startedAt ? startedAt + maxDurationS * 1000 : null),
    idlePolicy: normIdle(pick(raw, 'idle_policy', 'idlePolicy')),
    remainingS: numOrNull(pick(raw, 'remaining_s', 'remainingS')),
    stats: normStats(statsRaw),
    statsError: typeof statsErr === 'string' ? statsErr : null,
    pane: normPane(pick(raw, 'pane')),
  };
}

function normSession(o: Json): ClaudeSession {
  return {
    sessionId: String(pick(o, 'session_id', 'sessionId') ?? ''),
    project: String(pick(o, 'project') ?? ''),
    model: String(pick(o, 'model') ?? ''),
    turns: num(pick(o, 'turns')),
    inputTokens: num(pick(o, 'input_tokens', 'inputTokens')),
    outputTokens: num(pick(o, 'output_tokens', 'outputTokens')),
    cacheReadTokens: num(pick(o, 'cache_read_tokens', 'cacheReadTokens')),
    cacheCreationTokens: num(pick(o, 'cache_creation_tokens', 'cacheCreationTokens')),
    firstTs: toMs(pick(o, 'first_ts', 'firstTs')),
    lastTs: toMs(pick(o, 'last_ts', 'lastTs')),
    estCostUsd: numOrNull(pick(o, 'est_cost_usd', 'estCostUsd')),
    pricedAs: (pick(o, 'priced_as', 'pricedAs') as string | undefined) ?? null,
  };
}

export function normUsage(raw: Json): ClaudeUsage {
  const list = (pick(raw, 'sessions') as Json[] | undefined) ?? [];
  const sessions = list.map(normSession).sort((a, b) => (b.lastTs ?? 0) - (a.lastTs ?? 0));
  const t = (pick(raw, 'totals', 'total') as Json | undefined) ?? {};
  const sum = (f: (s: ClaudeSession) => number) => sessions.reduce((acc, s) => acc + f(s), 0);
  const totalCost = numOrNull(pick(t, 'est_cost_usd', 'estCostUsd') ?? pick(raw, 'est_cost_usd', 'total_est_cost_usd'));
  const pricing = (pick(raw, 'pricing') as Json | undefined) ?? {};
  const unpriced = pick(raw, 'unpriced_models', 'unpricedModels');
  return {
    unpricedModels: Array.isArray(unpriced) ? unpriced.map(String) : [],
    pricingSource: (pick(pricing, 'source') as string | undefined) ?? null,
    pricingAsOf: (pick(pricing, 'as_of', 'asOf') as string | undefined) ?? null,
    pricingNote: (pick(pricing, 'note') as string | undefined) ?? null,
    sessions,
    totals: {
      turns: num(pick(t, 'turns'), sum((s) => s.turns)),
      inputTokens: num(pick(t, 'input_tokens', 'inputTokens'), sum((s) => s.inputTokens)),
      outputTokens: num(pick(t, 'output_tokens', 'outputTokens'), sum((s) => s.outputTokens)),
      cacheReadTokens: num(pick(t, 'cache_read_tokens', 'cacheReadTokens'), sum((s) => s.cacheReadTokens)),
      cacheCreationTokens: num(pick(t, 'cache_creation_tokens', 'cacheCreationTokens'), sum((s) => s.cacheCreationTokens)),
      estCostUsd:
        totalCost ?? (sessions.some((s) => s.estCostUsd !== null) ? sum((s) => s.estCostUsd ?? 0) : null),
    },
  };
}

function credStatus(o: Json): CredentialStatus {
  const s = pick(o, 'status');
  if (s === 'active' || s === 'revoking' || s === 'revoked' || s === 'restoring' || s === 'unknown') return s;
  if (o.revoked === 'pending') return 'revoking';
  return o.revoked === true ? 'revoked' : o.revoked === false ? 'active' : 'unknown';
}

function normCredential(o: Json): Credential {
  const hostsRaw = pick(o, 'hosts');
  const hosts: Record<string, number> = {};
  if (hostsRaw && typeof hostsRaw === 'object' && !Array.isArray(hostsRaw)) {
    for (const [h, n] of Object.entries(hostsRaw as Json)) hosts[h] = num(n);
  }
  return {
    name: String(pick(o, 'name') ?? ''),
    kind: pick(o, 'kind') === 'custom' ? 'custom' : 'builtin',
    placeholderPrefix: String(pick(o, 'placeholder_prefix', 'placeholderPrefix') ?? ''),
    // The backend sends revoked: "pending" while a revoke is in flight; status says which way it's going.
    revoked: o.revoked === null || o.revoked === undefined ? null : o.revoked === 'pending' ? true : Boolean(o.revoked),
    status: credStatus(o),
    statusDetail: (pick(o, 'status_detail', 'statusDetail') as string | null | undefined) ?? null,
    statusSince: toMs(pick(o, 'status_since', 'statusSince')),
    uses15m: num(pick(o, 'uses_15m', 'uses15m')),
    hosts,
    lastUsed: toMs(pick(o, 'last_used', 'lastUsed')),
  };
}

function listOf(raw: unknown, ...keys: string[]): Json[] {
  if (Array.isArray(raw)) return raw as Json[];
  const inner = pick(raw as Json, ...keys);
  return Array.isArray(inner) ? (inner as Json[]) : [];
}

function itemOrNull(raw: unknown): Sandbox | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const o = raw as Json;
  return o.state || o.status ? normSandbox(o) : null;
}


// ---------- proxy policy (control plane) ----------

function strOrNull(v: unknown): string | null {
  return typeof v === 'string' && v !== '' ? v : null;
}

function strList(v: unknown): string[] {
  return Array.isArray(v) ? v.map(String) : [];
}

export function normApply(raw: unknown): ApplyRef | null {
  if (!raw || typeof raw !== 'object') return null;
  const o = raw as Json;
  return {
    configVersion: numOrNull(pick(o, 'config_version', 'configVersion')),
    scopeVersion: numOrNull(pick(o, 'scope_version', 'scopeVersion')),
    requestedAt: toMs(pick(o, 'requested_at', 'requestedAt')),
  };
}

function normOverview(r: Json): PolicyOverview {
  const limits = (pick(r, 'limits') as Json | undefined) ?? {};
  return {
    enabled: r.enabled === true,
    reason: strOrNull(r.reason),
    editable: r.editable === true,
    note: strOrNull(r.note),
    configVersion: numOrNull(pick(r, 'config_version', 'configVersion')),
    scopeVersion: numOrNull(pick(r, 'scope_version', 'scopeVersion')),
    region: strOrNull(r.region),
    limits: {
      policyChars: num(pick(limits, 'policy_chars', 'policyChars'), 2048),
      runPayloadBytes: num(pick(limits, 'run_payload_bytes', 'runPayloadBytes'), 4096),
      runPayloadBytesUsed: numOrNull(pick(limits, 'run_payload_bytes_used', 'runPayloadBytesUsed')),
    },
  };
}

function normAllowHost(o: Json): AllowHost {
  return { host: String(o.host ?? ''), managed: strOrNull(o.managed), service: strOrNull(o.service) };
}

function normPolicyCredential(o: Json): PolicyCredential {
  const kind = pick(o, 'kind') === 'custom' ? 'custom' : 'builtin';
  return {
    name: String(o.name ?? ''),
    kind,
    env: strOrNull(o.env),
    placeholder: strOrNull(o.placeholder),
    hosts: strList(o.hosts),
    headers: strList(o.headers),
    query: o.query === true,
    body: o.body === true,
    secretName: strOrNull(pick(o, 'secret_name', 'secretName')),
    status: credStatus(o),
    statusDetail: strOrNull(pick(o, 'status_detail', 'statusDetail')),
    onAllowlist: typeof o.on_allowlist === 'boolean' ? o.on_allowlist : null,
    createdAt: toMs(pick(o, 'created_at', 'createdAt')),
    createdBy: strOrNull(pick(o, 'created_by', 'createdBy')),
    rotatedAt: toMs(pick(o, 'rotated_at', 'rotatedAt')),
    rotatedBy: strOrNull(pick(o, 'rotated_by', 'rotatedBy')),
    rotatable: o.rotatable === undefined ? kind === 'custom' : o.rotatable === true,
    deletable: o.deletable === undefined ? kind === 'custom' : o.deletable === true,
  };
}

const LEVELS: AccessLevel[] = ['List', 'Read', 'Write', 'Permissions', 'Tagging'];
function level(v: unknown): AccessLevel {
  const s = String(v ?? '');
  return (LEVELS as string[]).includes(s) ? (s as AccessLevel) : 'Read';
}

export function normAwsAction(o: Json, service?: string): AwsAction {
  const action = String(o.action ?? '');
  const svc = String(o.service ?? service ?? action.split(':')[0] ?? '');
  return {
    action,
    service: svc,
    name: String(o.name ?? action.split(':')[1] ?? action),
    level: level(o.level),
    allowed: o.allowed === true,
    limited: o.limited === true,
    reason: strOrNull(o.reason),
  };
}

function normRole(o: Json): AwsRole {
  return {
    id: String(o.id ?? o.name ?? ''),
    description: String(o.description ?? ''),
    default: o.default === true,
    warning: strOrNull(o.warning),
  };
}

function normSummary(o: Json): AwsServiceSummary {
  const levels: Partial<Record<AccessLevel, number>> = {};
  const lv = (o.levels as Json | undefined) ?? {};
  for (const l of LEVELS) if (lv[l] !== undefined) levels[l] = num(lv[l]);
  return {
    id: String(o.id ?? ''),
    name: String(o.name ?? o.label ?? o.id ?? ''),
    actions: num(o.actions),
    allowed: num(o.allowed),
    levels,
    enabled: o.enabled !== false,
    reason: strOrNull(o.reason),
    signingNames: strList(pick(o, 'signing_names', 'signingNames')),
  };
}

function normDetail(o: Json): AwsServiceDetail {
  const id = String(o.id ?? '');
  return {
    id,
    name: String(o.name ?? id),
    enabled: o.enabled !== false,
    reason: strOrNull(o.reason),
    signingNames: strList(pick(o, 'signing_names', 'signingNames')),
    hosts: strList(o.hosts),
    actions: listOf(o, 'actions').map((a) => normAwsAction(a, id)),
    wildcards: listOf(o, 'wildcards').map((w) => ({
      action: String(w.action ?? ''),
      label: String(w.label ?? w.action ?? ''),
      covers: num(w.covers),
      allowed: w.allowed === true,
      reason: strOrNull(w.reason),
    })),
  };
}

function normSource(o: unknown): CatalogSource {
  const r = (o && typeof o === 'object' ? o : {}) as Json;
  return {
    url: strOrNull(r.url),
    fetchedAt: toMs(pick(r, 'fetched_at', 'fetchedAt')),
    ageS: numOrNull(pick(r, 'age_s', 'ageS')),
    stale: r.stale === true,
    error: strOrNull(r.error),
    services: numOrNull(r.services),
    actions: numOrNull(r.actions),
    fetchS: numOrNull(pick(r, 'fetch_s', 'fetchS')),
  };
}

function normCeiling(o: unknown): CatalogCeiling {
  const r = (o && typeof o === 'object' ? o : {}) as Json;
  return {
    role: strOrNull(r.role),
    policies: strList(r.policies),
    policy: strOrNull(r.policy),
    version: strOrNull(r.version),
    fetchedAt: toMs(pick(r, 'fetched_at', 'fetchedAt')),
    ageS: numOrNull(pick(r, 'age_s', 'ageS')),
    stale: r.stale === true,
    error: strOrNull(r.error),
  };
}

export function normPreview(raw: unknown): AwsPreview | null {
  if (!raw || typeof raw !== 'object') return null;
  const o = raw as Json;
  const policy = o.policy && typeof o.policy === 'object' ? (o.policy as Record<string, unknown>) : null;
  const policyJson = typeof o.policy_json === 'string' ? o.policy_json : policy ? JSON.stringify(policy) : '';
  const limit = num(o.limit, 2048);
  const chars = num(o.chars, policyJson.length);
  const sp = pick(o, 'session_policy', 'sessionPolicy');
  return {
    role: strOrNull(o.role),
    // Older backends always sent a policy
    sessionPolicy: typeof sp === 'boolean' ? sp : policy !== null,
    actions: strList(o.actions),
    services: strList(o.services),
    allowedServices: strList(pick(o, 'allowed_services', 'allowedServices')),
    hosts: strList(o.hosts),
    policy,
    policyJson,
    chars,
    limit,
    fits: typeof o.fits === 'boolean' ? o.fits : chars <= limit,
  };
}

function normScope(o: Json): AwsScope {
  const sp = pick(o, 'session_policy', 'sessionPolicy');
  return {
    version: numOrNull(o.version),
    role: strOrNull(o.role),
    sessionPolicy: typeof sp === 'boolean' ? sp : strList(o.actions).length > 0,
    actions: strList(o.actions),
    services: strList(o.services),
    updatedAt: toMs(pick(o, 'updated_at', 'updatedAt')),
    updatedBy: strOrNull(pick(o, 'updated_by', 'updatedBy')),
    preview: normPreview(o.preview),
  };
}

export function normStatus(r: Json): PolicyStatus {
  const target = normApply(r.target) ?? { configVersion: null, scopeVersion: null, requestedAt: null };
  const config = (r.config as Json | undefined) ?? {};
  const scope = (r.scope as Json | undefined) ?? {};
  const reload = (r.reload as Json | undefined) ?? {};
  const sidecar = (r.sidecar as Json | undefined) ?? {};
  return {
    enabled: r.enabled !== false,
    applied: r.applied === true,
    pending: r.pending === true,
    target,
    latencyS: numOrNull(pick(r, 'latency_s', 'latencyS')),
    config: { version: numOrNull(config.version), appliedAt: toMs(pick(config, 'applied_at', 'appliedAt')) },
    scope: {
      version: numOrNull(scope.version),
      role: strOrNull(scope.role),
      sessionPolicy: typeof pick(scope, 'session_policy', 'sessionPolicy') === 'boolean' ? (pick(scope, 'session_policy', 'sessionPolicy') as boolean) : null,
      sessionName: strOrNull(pick(scope, 'session_name', 'sessionName')),
      sourceIdentity: strOrNull(pick(scope, 'source_identity', 'sourceIdentity')),
      expiration: toMs(scope.expiration),
      assumedAt: toMs(pick(scope, 'assumed_at', 'assumedAt')),
      sessionTokenUtilization: numOrNull(pick(scope, 'session_token_utilization', 'sessionTokenUtilization')),
      sessionTokenSize: numOrNull(pick(scope, 'session_token_size', 'sessionTokenSize')),
      packedPolicySize: numOrNull(pick(scope, 'packed_policy_size', 'packedPolicySize')),
      error: strOrNull(scope.error),
    },
    reload: {
      at: toMs(reload.at),
      ok: typeof reload.ok === 'boolean' ? reload.ok : null,
      httpStatus: numOrNull(pick(reload, 'http_status', 'httpStatus')),
      error: strOrNull(reload.error),
    },
    sidecar: {
      task: strOrNull(sidecar.task),
      updatedAt: toMs(pick(sidecar, 'updated_at', 'updatedAt')),
      heartbeatAgeS: numOrNull(pick(sidecar, 'heartbeat_age_s', 'heartbeatAgeS')),
      stale: sidecar.stale === true,
    },
    error: strOrNull(r.error),
  };
}

function normChange(o: Json): PolicyChange {
  return {
    id: String(o.id ?? `${o.at ?? ''}`),
    at: toMs(o.at),
    actor: strOrNull(o.actor),
    kind: String(o.kind ?? ''),
    summary: String(o.summary ?? o.kind ?? ''),
    detail: o.detail && typeof o.detail === 'object' ? (o.detail as Record<string, unknown>) : null,
    configVersion: numOrNull(pick(o, 'config_version', 'configVersion')),
    scopeVersion: numOrNull(pick(o, 'scope_version', 'scopeVersion')),
  };
}

/** What a policy write returns that the page cares about: the change to track, and any warnings. */
export interface WriteResult {
  apply: ApplyRef | null;
  change: PolicyChange | null;
  warnings: string[];
  raw: Json;
}

function writeResult(r: Json | null): WriteResult {
  const o = r ?? {};
  return {
    apply: normApply(o.apply),
    change: o.change && typeof o.change === 'object' ? normChange(o.change as Json) : null,
    warnings: strList(o.warnings),
    raw: o,
  };
}

const enc = encodeURIComponent;

// ---------- endpoints ----------

export const api = {
  me: async (): Promise<Me> => {
    const r = await request<Json>('GET', '/api/me');
    const groups = Array.isArray(r.groups) ? r.groups.map(String) : [];
    return { sub: String(r.sub ?? ''), email: String(r.email ?? ''), groups, isAdmin: r.is_admin === true || groups.includes('admins') };
  },
  config: async (): Promise<AppConfig> => {
    const r = await request<Json>('GET', '/api/config');
    const allow = pick(r, 'proxy_allowlist', 'allowlist');
    const pricing = (pick(r, 'pricing') as Json | undefined) ?? {};
    return {
      proxyMode: String(pick(r, 'proxy_mode', 'proxyMode') ?? 'explicit'),
      quotaGb: num(pick(r, 'quota_gb', 'quotaGb'), 8),
      maxDurationS: num(pick(r, 'max_duration_s', 'maxDurationS'), 8 * 3600),
      pricing,
      allowlist: Array.isArray(allow) ? allow.map(String) : null,
      vmMemoryGb: num(pick(r, 'vm_memory_gb', 'vmMemoryGb'), 2),
      defaultIdleMinutes: num(pick(r, 'default_idle_minutes', 'defaultIdleMinutes'), 15),
      pricingSource: (pick(pricing, 'source') as string | undefined) ?? null,
      pricingAsOf: (pick(pricing, 'as_of', 'asOf') as string | undefined) ?? null,
      sandboxCidrs: (() => {
        const c = pick(r, 'sandbox_cidrs', 'sandboxCidrs');
        return Array.isArray(c) ? c.map(String) : [];
      })(),
    };
  },
  sandboxes: async (): Promise<Sandbox[]> => {
    const r = await request<unknown>('GET', '/api/sandboxes');
    return listOf(r, 'sandboxes', 'items').map(normSandbox);
  },
  createSandbox: async (body: { name: string; idle_minutes: number; max_hours: number }): Promise<Sandbox> =>
    normSandbox(await request<Json>('POST', '/api/sandboxes', body)),
  // Each returns the sandbox item with the state right after the call (or null if the body isn't one).
  suspend: async (id: string) => itemOrNull(await request('POST', `/api/sandboxes/${encodeURIComponent(id)}/suspend`)),
  resume: async (id: string) => itemOrNull(await request('POST', `/api/sandboxes/${encodeURIComponent(id)}/resume`)),
  terminate: async (id: string) => itemOrNull(await request('DELETE', `/api/sandboxes/${encodeURIComponent(id)}`)),
  vitals: async (id: string, live: boolean): Promise<Vitals> =>
    normVitals(await request<Json>('GET', `/api/sandboxes/${encodeURIComponent(id)}/vitals?live=${live ? 1 : 0}`)),
  claudeUsage: async (id: string): Promise<ClaudeUsage> =>
    normUsage(await request<Json>('GET', `/api/sandboxes/${encodeURIComponent(id)}/claude-usage`)),
  quota: async (): Promise<Quota> => {
    const r = await request<Json>('GET', '/api/quota');
    return {
      usedGb: num(pick(r, 'used_gb', 'usedGb')),
      quotaGb: num(pick(r, 'quota_gb', 'quotaGb'), 8),
      userGb: numOrNull(pick(r, 'user_gb', 'userGb')),
      vmMemoryGb: num(pick(r, 'vm_memory_gb', 'vmMemoryGb'), 2),
    };
  },
  credentials: async (): Promise<Credential[]> => {
    const r = await request<unknown>('GET', '/api/credentials');
    return listOf(r, 'credentials', 'items').map(normCredential);
  },
  revoke: (name: string) => request<Json>('POST', `/api/credentials/${encodeURIComponent(name)}/revoke`),
  restore: (name: string) => request<Json>('POST', `/api/credentials/${encodeURIComponent(name)}/restore`),

  // ---- proxy policy
  policy: async (): Promise<PolicyOverview> => normOverview(await request<Json>('GET', '/api/policy')),
  allowlist: async (): Promise<Allowlist> => {
    const r = await request<Json>('GET', '/api/policy/allowlist');
    return { version: numOrNull(r.version), hosts: listOf(r, 'hosts').map(normAllowHost) };
  },
  allowHost: async (host: string) => writeResult(await request<Json>('POST', '/api/policy/allowlist', { host })),
  removeHost: async (host: string) => writeResult(await request<Json>('DELETE', `/api/policy/allowlist/${enc(host)}`)),
  policyCredentials: async (): Promise<PolicyCredential[]> =>
    listOf(await request<unknown>('GET', '/api/policy/credentials'), 'credentials', 'items').map(normPolicyCredential),
  createCredential: async (c: NewCredential) => writeResult(await request<Json>('POST', '/api/policy/credentials', c)),
  rotateCredential: async (name: string, value: string) =>
    writeResult(await request<Json>('POST', `/api/policy/credentials/${enc(name)}/rotate`, { value })),
  deleteCredential: async (name: string) => writeResult(await request<Json>('DELETE', `/api/policy/credentials/${enc(name)}`)),
  revokeCredential: async (name: string) => writeResult(await request<Json>('POST', `/api/credentials/${enc(name)}/revoke`)),
  restoreCredential: async (name: string) => writeResult(await request<Json>('POST', `/api/credentials/${enc(name)}/restore`)),
  awsCatalog: async (role?: string | null): Promise<AwsCatalog> => {
    const r = await request<Json>('GET', `/api/policy/aws/catalog${role ? `?role=${enc(role)}` : ''}`);
    const limits = (r.limits as Json | undefined) ?? {};
    const levels = strList(r.levels).filter((l): l is AccessLevel => (LEVELS as string[]).includes(l));
    const ceiling = normCeiling(r.ceiling);
    return {
      region: strOrNull(r.region),
      role: strOrNull(r.role) ?? ceiling.role,
      roles: listOf(r, 'roles').map(normRole),
      source: normSource(r.source),
      ceiling,
      levels: levels.length ? levels : LEVELS,
      services: listOf(r, 'services').map(normSummary),
      baseline: r.baseline && typeof r.baseline === 'object' ? (r.baseline as Record<string, unknown>) : null,
      policyChars: num(pick(limits, 'policy_chars', 'policyChars'), 2048),
    };
  },
  awsService: async (id: string, role?: string | null): Promise<AwsServiceDetail> =>
    normDetail(await request<Json>('GET', `/api/policy/aws/services/${enc(id)}${role ? `?role=${enc(role)}` : ''}`)),
  awsSearch: async (q: string, lvl: AccessLevel | null, limit = 100, role?: string | null): Promise<AwsSearchResult> => {
    const qs = new URLSearchParams({ q, limit: String(limit) });
    if (lvl) qs.set('level', lvl);
    if (role) qs.set('role', role);
    const r = await request<Json>('GET', `/api/policy/aws/search?${qs}`);
    return {
      q: String(r.q ?? q),
      level: r.level ? level(r.level) : null,
      total: num(r.total),
      truncated: r.truncated === true,
      services: listOf(r, 'services').map((o) => ({
        id: String(o.id ?? ''),
        name: String(o.name ?? o.id ?? ''),
        enabled: o.enabled !== false,
        matches: num(o.matches),
      })),
      actions: listOf(r, 'actions').map((a) => normAwsAction(a)),
    };
  },
  awsScope: async (): Promise<AwsScope> => normScope(await request<Json>('GET', '/api/policy/aws/scope')),
  awsPreview: async (s: AwsScopeRequest): Promise<AwsPreview> =>
    normPreview(await request<Json>('POST', '/api/policy/aws/preview', scopeBody(s)))!,
  applyAwsScope: async (s: AwsScopeRequest) => {
    const r = await request<Json>('PUT', '/api/policy/aws/scope', scopeBody(s));
    return { ...writeResult(r), unchanged: r.unchanged === true, preview: normPreview(r.preview) };
  },
  policyStatus: async (): Promise<PolicyStatus> => normStatus(await request<Json>('GET', '/api/policy/status')),
  policyChanges: async (limit = 50): Promise<PolicyChange[]> =>
    listOf(await request<unknown>('GET', `/api/policy/changes?limit=${limit}`), 'changes', 'items').map(normChange),
};

/** What the AWS access tab sends to preview and apply. */
export interface AwsScopeRequest {
  role: string | null;
  sessionPolicy: boolean;
  actions: string[];
}

function scopeBody(s: AwsScopeRequest) {
  return { ...(s.role ? { role: s.role } : {}), session_policy: s.sessionPolicy, actions: s.actions };
}

/** Pulls a human warning out of a revoke/restore response, if the backend sent one. */
export function warningOf(r: unknown): string | null {
  if (!r || typeof r !== 'object') return null;
  const w = pick(r as Json, 'warning', 'message', 'detail');
  return typeof w === 'string' ? w : null;
}
