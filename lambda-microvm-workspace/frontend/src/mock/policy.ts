// Mock of the proxy control plane (backend/API.md "Proxy policy"). Every value is synthetic. Writes bump the
// config/scope version and the fake sidecar applies them at its next check, like part 2's: it checks the bucket
// every 30 s (?poll=5 for a faster mock) and reloads about a second later.

import { delay, http, HttpResponse } from 'msw';
import { ALLOWLIST, db, gates, PLACEHOLDERS } from './db';
import { actionsFor, BY_ID, coverageOf, DEFAULT_ROLE, openLists, resolve, roleOf, ROLES, search, SERVICES, summary, TOTAL_ACTIONS, wildcards, type Level, type RoleId } from './awsCatalog';

const REGION = 'us-east-1';
const ACCOUNT = '111122223333'; // AWS's documentation example account
const VIEWER = new URLSearchParams(window.location.search).get('viewer') === '1';
export const MOCK_EMAIL = 'dev@example.com';
const latency = () => delay(80 + Math.random() * 220);
const iso = (ms: number | null) => (ms === null ? null : new Date(ms).toISOString());
const hex = (n: number) => Array.from({ length: n }, () => Math.floor(Math.random() * 16).toString(16)).join('');

export const IS_ADMIN = !VIEWER;
const POLL_S = Math.max(1, Number(new URLSearchParams(window.location.search).get('poll')) || 30);

// ---------------------------------------------------------------- catalog (see awsCatalog.ts)

const BASELINE = {
  Sid: 'ProxySecrets',
  Effect: 'Allow',
  Action: 'secretsmanager:GetSecretValue',
  Resource: ['anthropic-*', 'github-*', 'demo-*', 'user/*'].map((s) => `arn:aws:secretsmanager:${REGION}:${ACCOUNT}:secret:my2cents-ip/${s}`),
};

function preview(role: RoleId, sessionPolicy: boolean, actions: string[]) {
  const acts = [...new Set(actions)].sort();
  if (!sessionPolicy) {
    // No session policy: the role's full permissions; iron-proxy gets the services the role allows something in
    const open = openLists(role);
    const effective = SERVICES.filter((s) => s.enabled && s.id !== 'secretsmanager')
      .map((s) => {
        const acts2 = actionsFor(s, role);
        const ok = acts2.filter((a) => a.allowed);
        return { service: s.id, name: s.name, asked: null, effective: ok.length, role_allows: ok.length, total: acts2.length, limited: ok.filter((a) => a.limited).length, actions: null, limited_actions: null, no_effect: false, reason: null };
      })
      .filter((r) => r.effective > 0);
    return { role, session_policy: false, actions: acts, services: [], ...open, policy: null, policy_json: null, chars: 0, limit: 2048, fits: true, coverage: [], effective, deny_ssm: false };
  }
  const resolved = acts.map((a) => [a, resolve(a, role)] as const);
  const problems = resolved.filter(([, r]) => typeof r === 'string').map(([, r]) => r as string);
  if (problems.length) return { error: `actions: can't be applied: ${problems.join('; ')}` };
  const coverage = [];
  const asked = new Map<string, Map<string, ReturnType<typeof actionsFor>[number]>>();
  let deny = false;
  for (const [a, r] of resolved) {
    if (typeof r === 'string') continue;
    const cov = coverageOf(r.covered, role);
    deny = deny || cov.blocked > 0;
    coverage.push({ action: a, kind: r.wildcard ? 'wildcard' : 'action', covers: cov.covers, effective: cov.effective, limited: cov.limited, tone: cov.tone, reason: cov.reason });
    const sid = a.split(':')[0];
    const m = asked.get(sid) ?? new Map();
    for (const c of r.covered) m.set(c.action, c);
    asked.set(sid, m);
  }
  const effective = [...asked.entries()].sort(([x], [y]) => x.localeCompare(y)).map(([sid, m]) => {
    const s = BY_ID.get(sid)!;
    const covered = [...m.values()];
    const ok = covered.filter((c) => c.allowed);
    return {
      service: sid,
      name: s.name,
      asked: covered.length,
      effective: ok.length,
      role_allows: actionsFor(s, role).filter((a) => a.allowed).length,
      total: s.actions.length,
      limited: ok.filter((c) => c.limited).length,
      actions: ok.map((c) => c.action).sort(),
      limited_actions: ok.filter((c) => c.limited).map((c) => c.action).sort(),
      no_effect: ok.length === 0,
      reason: ok.length ? null : `no effect with ${role}: no picked action in it is allowed, so iron-proxy refuses its calls (a bare 403) before they reach AWS`,
    };
  });
  const svcs = effective.filter((r) => r.effective > 0).map((r) => BY_ID.get(r.service)!);
  const statements: Record<string, unknown>[] = [BASELINE];
  if (acts.length) statements.push({ Sid: 'Scope', Effect: 'Allow', Action: acts, Resource: '*' });
  if (acts.length && deny) statements.push({ Sid: 'NoParameterValues', Effect: 'Deny', Action: ['ssm:GetParameter*'], Resource: '*' });
  const policy = { Version: '2012-10-17', Statement: statements };
  const json = JSON.stringify(policy);
  return {
    role,
    session_policy: true,
    actions: acts,
    services: [...asked.keys()].sort(),
    allowed_services: svcs.length ? [...new Set(svcs.flatMap((s) => s.signing))].sort() : ['none'],
    hosts: [...new Set(svcs.flatMap((s) => s.hosts))],
    policy,
    policy_json: json,
    chars: json.length,
    limit: 2048,
    fits: json.length <= 2048,
    deny_ssm: deny,
    coverage,
    effective,
  };
}

const current = () => preview(state.role, state.sessionPolicy, state.actions);

// ---------------------------------------------------------------- state

interface MCred {
  name: string;
  kind: 'builtin' | 'custom';
  env: string;
  placeholder: string;
  hosts: string[];
  headers: string[];
  query: boolean;
  body: boolean;
  created_at: number | null;
  created_by: string | null;
  rotated_at: number | null;
  rotated_by: string | null;
  /** In-flight revoke/restore: settles when the sidecar applies `config`. */
  op: { op: 'revoke' | 'restore'; config: number; since: number } | null;
}

interface MChange {
  id: string;
  at: number;
  actor: string;
  kind: string;
  summary: string;
  detail: Record<string, unknown>;
  config_version: number;
  scope_version: number;
}

const t0 = Date.now();
const state = {
  config: 9,
  scope: 3,
  requestedAt: t0 - 21 * 60_000,
  allow: ALLOWLIST.filter((h) => !h.endsWith('.amazonaws.com')),
  actions: ['ecs:DescribeClusters', 'ecs:ListClusters', 'ecs:ListServices', 's3:ListAllMyBuckets', 'sts:GetCallerIdentity'],
  role: DEFAULT_ROLE as RoleId,
  sessionPolicy: false,
  scopeAt: t0 - 21 * 60_000,
  scopeBy: 'ops@example.com',
  applied: {
    config: 9,
    scope: 3,
    at: t0 - 21 * 60_000 + 2300,
    latency: 2.3,
    assumedAt: t0 - 21 * 60_000 + 1900,
    expiration: t0 - 21 * 60_000 + 3600_000,
    packed: 7,
    role: DEFAULT_ROLE as RoleId,
    sessionPolicy: false,
    reloadOk: true as boolean,
    reloadError: null as string | null,
  },
  creds: [
    builtin('anthropic', 'ANTHROPIC_API_KEY', ['api.anthropic.com'], ['X-Api-Key', 'Authorization']),
    builtin('github', 'GH_TOKEN', ['api.github.com', 'github.com'], ['Authorization']),
    builtin('demo', 'DEMO_TOKEN', ['httpbin.org'], [], true, true),
    {
      name: 'httpbin-test',
      kind: 'custom',
      env: 'TEST_KEY',
      placeholder: `m2c-ph-httpbin-test-${hex(16)}`,
      hosts: ['httpbin.org'],
      headers: ['X-Test-Key'],
      query: false,
      body: false,
      created_at: t0 - 48 * 60_000,
      created_by: 'ops@example.com',
      rotated_at: t0 - 12 * 60_000,
      rotated_by: 'ops@example.com',
      op: null,
    } as MCred,
  ],
  changes: [] as MChange[],
};

function builtin(name: string, env: string, hosts: string[], headers: string[], query = false, body = false): MCred {
  return { name, kind: 'builtin', env, placeholder: PLACEHOLDERS[name] ?? `${name}-placeholder`, hosts, headers, query, body, created_at: null, created_by: null, rotated_at: null, rotated_by: null, op: null };
}

function seedChanges() {
  const mk = (minAgo: number, actor: string, kind: string, summary: string, cfg: number, scope: number, detail: Record<string, unknown> = {}) => {
    const at = t0 - minAgo * 60_000;
    state.changes.push({ id: `${new Date(at).toISOString()}#${hex(6)}`, at, actor, kind, summary, detail, config_version: cfg, scope_version: scope });
  };
  mk(21, 'ops@example.com', 'aws.scope', 'AWS access: role view-only, no session policy', 9, 3, { role: 'view-only', session_policy: false });
  mk(34, 'ops@example.com', 'credential.rotate', 'Rotated httpbin-test', 8, 2, { name: 'httpbin-test' });
  mk(48, 'ops@example.com', 'credential.create', 'Added credential httpbin-test (TEST_KEY) for httpbin.org', 8, 2, { name: 'httpbin-test' });
  mk(63, 'dev@example.com', 'allowlist.add', 'Allowed files.pythonhosted.org', 7, 2, { host: 'files.pythonhosted.org' });
  mk(64, 'dev@example.com', 'allowlist.add', 'Allowed pypi.org', 6, 2, { host: 'pypi.org' });
  mk(90, 'ops@example.com', 'allowlist.remove', 'Removed example.com', 5, 2, { host: 'example.com' });
  mk(95, 'ops@example.com', 'allowlist.add', 'Allowed example.com', 4, 2, { host: 'example.com' });
  mk(140, 'ops@example.com', 'aws.scope', 'AWS access: sts, s3 (2 actions)', 3, 2, { services: ['sts', 's3'] });
}
seedChanges();
state.changes.sort((a, b) => b.at - a.at);

function awsHosts(): { host: string; service: string }[] {
  const p = current();
  if ('error' in p) return [];
  if (!p.session_policy) return p.hosts.map((host) => ({ host, service: host.replace(/^\*\./, '').split('.')[0] }));
  return p.services.flatMap((id) => (BY_ID.get(id)?.hosts ?? []).map((host) => ({ host, service: id })));
}

function matches(pattern: string, host: string) {
  return pattern.startsWith('*.') ? host === pattern.slice(2) || host.endsWith(pattern.slice(1)) : pattern === host;
}

export function currentAllowlist(): string[] {
  return [...state.allow, ...awsHosts().map((h) => h.host)];
}

// The traffic generator follows this policy.
gates.allowed = (host) => currentAllowlist().some((p) => matches(p, host));
gates.swappable = (name) => state.creds.some((c) => c.name === name);
gates.awsService = (signing) => {
  const p = current();
  return !('error' in p) && p.allowed_services.includes(signing);
};

// ---------------------------------------------------------------- the fake sidecar

const sidecarTask = hex(32);
const sidecarStarted = t0 - 3 * 3600_000 - 17_000;
/** Checks happen every POLL_S on a fixed cadence from the task's start; each round reports right after. */
const roundAt = (k: number) => sidecarStarted + k * POLL_S * 1000;
function lastRound(now = Date.now()) {
  return roundAt(Math.floor((now - sidecarStarted) / (POLL_S * 1000)));
}
function nextRound(now = Date.now()) {
  return lastRound(now) + POLL_S * 1000;
}

function pending() {
  return state.applied.config < state.config || state.applied.scope < state.scope;
}

const etagOf = (s: string) => {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 0x01000193) >>> 0;
  return `"${h.toString(16).padStart(8, '0')}${(h ^ 0x5bd1e995).toString(16).padStart(8, '0')}${s.length.toString(16).padStart(8, '0')}"`;
};

function write(kind: string, summary: string, detail: Record<string, unknown>, scope = false): { change: MChange; apply: Record<string, unknown> } {
  state.config += 1;
  if (scope) state.scope += 1;
  state.requestedAt = Date.now();
  const change: MChange = {
    id: `${new Date().toISOString()}#${hex(6)}`,
    at: Date.now(),
    actor: MOCK_EMAIL,
    kind,
    summary,
    detail,
    config_version: state.config,
    scope_version: state.scope,
  };
  state.changes.unshift(change);
  const cfg = state.config;
  const sc = state.scope;
  const requested = state.requestedAt;
  // The next check after this write, plus the fetch and reload
  const applyAt = nextRound(requested) + 600 + Math.random() * 500;
  window.setTimeout(() => {
    if (state.config !== cfg || state.scope !== sc) return; // a newer change applies both
    const now = Date.now();
    const scopeChanged = sc !== state.applied.scope;
    state.applied = {
      ...state.applied,
      config: cfg,
      scope: sc,
      at: now,
      latency: (now - requested) / 1000,
      assumedAt: scopeChanged ? now - 400 : state.applied.assumedAt,
      expiration: scopeChanged ? now - 400 + 3600_000 : state.applied.expiration,
      packed: scopeChanged ? (state.sessionPolicy ? Math.max(3, Math.round(((current() as { chars?: number }).chars ?? 300) / 40)) : 12) : state.applied.packed,
      role: scopeChanged ? state.role : state.applied.role,
      sessionPolicy: scopeChanged ? state.sessionPolicy : state.applied.sessionPolicy,
      reloadOk: true,
      reloadError: null,
    };
    for (const c of state.creds) {
      if (c.op && c.op.config <= cfg) {
        if (c.op.op === 'revoke') db.revoked.add(c.name);
        else db.revoked.delete(c.name);
        c.op = null;
      }
    }
  }, applyAt - Date.now());
  return { change, apply: { config_version: state.config, scope_version: state.scope, requested_at: iso(state.requestedAt) } };
}

function sidecarView() {
  const now = Date.now();
  const updated = lastRound(now) + 350;
  const shown = updated > now ? updated - POLL_S * 1000 : updated;
  const next = shown + POLL_S * 1000 - 350;
  return {
    task: sidecarTask,
    started_at: iso(sidecarStarted),
    updated_at: iso(shown),
    heartbeat_age_s: Math.round((now - shown) / 100) / 10,
    stale: false,
    stale_after_s: 90,
    poll_s: POLL_S,
    next_poll_at: iso(next),
    next_poll_in_s: Math.max(0, Math.round((next - now) / 100) / 10),
  };
}

function scopeDoc() {
  const p = current();
  const sp = !('error' in p) && p.session_policy;
  return {
    version: state.scope,
    updated_at: iso(state.scopeAt),
    updated_by: state.scopeBy,
    role: state.role,
    session_policy: state.sessionPolicy,
    actions: state.sessionPolicy ? state.actions : [],
    services: sp ? (p as { services: string[] }).services : [],
    statements: sp ? [{ Sid: 'Scope', Effect: 'Allow', Action: state.actions, Resource: '*' }] : [],
  };
}

/** iron-proxy.yaml as the backend writes it: the header line, part 2's template, this mock's lists. */
function configYaml(): string {
  const p = current();
  const services = 'error' in p ? ['none'] : p.allowed_services;
  const q = (s: string) => JSON.stringify(s);
  const secret = (c: MCred) =>
    [
      '        - source:',
      '            type: aws_sm',
      `            secret_id: ${q(`arn:aws:secretsmanager:${REGION}:${ACCOUNT}:secret:my2cents-ip/${c.kind === 'custom' ? `user/${c.name}` : c.name}-AbCdEf`)}`,
      '            ttl: "30s"',
      ...(c.kind === 'custom' ? ['            failure_ttl: "5s"'] : []),
      '          replace:',
      `            proxy_value: ${q(c.placeholder)}`,
      `            match_headers: [${c.headers.map(q).join(', ')}]`,
      ...(c.query ? ['            match_query: true'] : []),
      ...(c.body ? ['            match_body: true'] : []),
      '          rules:',
      ...c.hosts.map((h) => `            - host: ${q(h)}`),
    ].join('\n');
  return [
    `# my2cents control plane: config v${state.config}, ${iso(state.requestedAt)}, ${state.changes[0]?.actor ?? MOCK_EMAIL}`,
    'dns:',
    '  # iron-proxy\'s own DNS server stays off: DNS Firewall in the sandbox VPC answers every name with a CNAME to the NLB.',
    '  enabled: false',
    '',
    'proxy:',
    '  http_listen: ":80"',
    '  https_listen: ":443"',
    '  tunnel_listen: ":8080"',
    '  upstream_response_header_timeout: "5m"',
    '  upstream_deny_cidrs:',
    ...['169.254.169.254/32', '169.254.170.2/32', 'fd00:ec2::254/128', '127.0.0.0/8', '::1/128', '10.50.0.0/16', '10.51.0.0/16', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'].map((c) => `    - ${q(c)}`),
    '',
    'tls:',
    '  mode: "mitm"',
    '  ca_cert: "/etc/iron-proxy/ca.crt"',
    '  ca_key: "/etc/iron-proxy/ca.key"',
    '',
    'metrics:',
    '  listen: ":9090"',
    '',
    '# control_plane=true: the control sidecar calls POST /v1/reload here after it syncs a new config or new',
    '# credentials. Loopback only, so it is never reachable through the NLB.',
    'management:',
    '  listen: "127.0.0.1:9092"',
    '  api_key_env: "IRON_MANAGEMENT_API_KEY"',
    '',
    'transforms:',
    '  - name: allowlist',
    '    config:',
    '      domains:',
    ...currentAllowlist().map((h) => `        - ${q(h)}`),
    '',
    '  - name: secrets',
    '    config:',
    '      secrets:',
    ...state.creds.map(secret),
    '',
    '  - name: aws_auth',
    '    config:',
    '      credentials_provider:',
    '        type: workload_identity',
    `        region: ${q(REGION)}`,
    `      allowed_services: [${services.map(q).join(', ')}]`,
    `      allowed_regions: [${q(REGION)}, "us-east-1"]`,
    '      rules:',
    '        - host: "*.amazonaws.com"',
    '',
    'log:',
    '  level: "info"',
    '',
  ].join('\n');
}

/** The mock's factory-reset plan, same shape as the backend's. */
function resetPlan() {
  const seeded = ALLOWLIST.filter((h) => !h.endsWith('.amazonaws.com'));
  const custom = state.creds.filter((c) => c.kind === 'custom');
  const revoked = state.creds.filter((c) => db.revoked.has(c.name) || c.op?.op === 'revoke').map((c) => c.name);
  const from = { role: state.role, session_policy: state.sessionPolicy, actions: state.actions };
  const to = { role: DEFAULT_ROLE, session_policy: false, actions: [] as string[] };
  const awsChanges = from.role !== to.role || from.session_policy || from.actions.length > 0;
  const remove = state.allow.filter((h) => !seeded.includes(h));
  const add = seeded.filter((h) => !state.allow.includes(h));
  return {
    config_version: state.config,
    next_config_version: state.config + 1,
    allowlist: { remove, add, after: seeded },
    credentials: {
      reactivate: revoked,
      remove: custom.map((c) => ({ name: c.name, env: c.env, hosts: c.hosts, secret_name: `my2cents-ip/user/${c.name}`, in_config: true })),
      builtin_changed: [],
      builtin: state.creds.filter((c) => c.kind === 'builtin').map((c) => c.name),
    },
    aws: { from, to, changes: awsChanges },
    changes: remove.length + add.length + custom.length + revoked.length + (awsChanges ? 1 : 0),
    note: `Applies to every sandbox on this proxy, running ones included, at the proxy's next check (within about ${POLL_S} s).`,
  };
}

function credStatus(c: MCred) {
  if (c.op) return c.op.op === 'revoke' ? 'revoking' : 'restoring';
  return db.revoked.has(c.name) ? 'revoked' : 'active';
}

function credView(c: MCred) {
  const status = credStatus(c);
  return {
    name: c.name,
    kind: c.kind,
    env: c.env,
    placeholder: c.placeholder,
    hosts: c.hosts,
    headers: c.headers,
    query: c.query,
    body: c.body,
    secret_name: c.kind === 'custom' ? `my2cents-ip/user/${c.name}` : null,
    status,
    revoked: status === 'revoking' ? 'pending' : status === 'revoked',
    status_detail: c.op ? `waiting for the proxy to apply config v${c.op.config}` : null,
    on_allowlist: c.hosts.every((h) => currentAllowlist().some((p) => matches(p, h) || p === h)),
    created_at: iso(c.created_at),
    created_by: c.created_by,
    rotated_at: iso(c.rotated_at),
    rotated_by: c.rotated_by,
    rotatable: c.kind === 'custom',
    deletable: c.kind === 'custom',
  };
}

/** Status for the dock's GET /api/credentials (kind and in-flight state come from here). */
export function credentialMeta(): { name: string; kind: string; status: string; placeholder: string }[] {
  return state.creds.map((c) => ({ name: c.name, kind: c.kind, status: credStatus(c), placeholder: c.placeholder }));
}

function payloadBytes(extra = 0) {
  return 1180 + state.creds.filter((c) => c.kind === 'custom').reduce((n, c) => n + c.env.length + c.placeholder.length + 6, 0) + extra;
}

// ---------------------------------------------------------------- validation

const SUFFIXES = new Set(['co.uk', 'org.uk', 'com.au', 'co.jp', 'github.io', 'herokuapp.com', 'cloudfront.net', 'vercel.app', 'pages.dev', 'workers.dev']);

function hostError(raw: string): string | null {
  const h = raw.trim().toLowerCase().replace(/\.$/, '');
  if (!h) return 'host is required';
  if (h === '*') return 'a bare * would allow every host';
  if (/^[0-9.]+$/.test(h) || h.includes(':')) return 'IP literals, ports and schemes aren’t allowed: use a host name';
  if (h.includes('/')) return 'just the host name: no scheme or path';
  if (h.includes('*') && !/^\*\.[^*]+$/.test(h)) return 'wildcards only as a leading *.';
  const bare = h.replace(/^\*\./, '');
  if (h.startsWith('*.') && (bare.split('.').length < 2 || SUFFIXES.has(bare))) return `*.${bare} would cover a public suffix`;
  if (!/^[a-z0-9-]+(\.[a-z0-9-]+)+$/.test(bare)) return 'not a valid host name';
  if (bare === 'amazonaws.com' || bare.endsWith('.amazonaws.com')) {
    return bare.startsWith('secretsmanager.') ? 'Secrets Manager endpoints are never allowed' : 'AWS endpoints follow AWS access; choose the service there';
  }
  return null;
}

const forbidden = () => HttpResponse.json({ detail: 'admins only: ask an admin to change the proxy policy' }, { status: 403 });
const problem = (status: number, detail: string) => HttpResponse.json({ detail }, { status });
const field = (loc: string, msg: string) => HttpResponse.json({ detail: [{ loc: ['body', loc], msg, type: 'value_error' }] }, { status: 422 });

// ---------------------------------------------------------------- handlers

export const policyHandlers = [
  http.get('/api/policy', async () => {
    await latency();
    return HttpResponse.json({
      enabled: true,
      editable: IS_ADMIN,
      note: 'Changes apply to every sandbox on this proxy.',
      config_version: state.config,
      scope_version: state.scope,
      region: REGION,
      limits: { policy_chars: 2048, run_payload_bytes: 4096, run_payload_bytes_used: payloadBytes() },
    });
  }),

  http.get('/api/policy/status', async () => {
    await delay(60 + Math.random() * 90);
    const p = pending();
    const a = state.applied;
    return HttpResponse.json({
      enabled: true,
      applied: !p && a.reloadOk,
      pending: p,
      target: { config_version: state.config, scope_version: state.scope, requested_at: iso(state.requestedAt) },
      latency_s: p ? null : Math.round(a.latency * 10) / 10,
      config: { version: a.config, applied_at: iso(a.at) },
      scope: {
        version: a.scope,
        role: a.role,
        session_policy: a.sessionPolicy,
        session_name: `ws-${a.role}-v${a.scope}`,
        source_identity: a.scope === state.scope && state.scopeBy ? state.scopeBy : MOCK_EMAIL,
        expiration: iso(a.expiration),
        assumed_at: iso(a.assumedAt),
        session_token_utilization: a.packed === null ? null : Math.min(100, a.packed + 1),
        session_token_size: a.packed === null ? null : 1100 + a.packed * 6,
        packed_policy_size: a.packed,
        error: null,
      },
      reload: { at: iso(a.at), ok: a.reloadOk, http_status: a.reloadOk ? 200 : 422, error: a.reloadError },
      sidecar: sidecarView(),
      sync: { state: p ? 'pending' : 'in_sync', behind: [a.config < state.config ? 'config' : null, a.scope < state.scope ? 'scope' : null].filter(Boolean) },
      error: null,
    });
  }),

  http.get('/api/policy/reset', async () => {
    await latency();
    if (!IS_ADMIN) return forbidden();
    return HttpResponse.json(resetPlan());
  }),

  http.post('/api/policy/reset', async ({ request }) => {
    await latency();
    if (!IS_ADMIN) return forbidden();
    const b = (await request.json()) as { confirm?: string };
    if ((b.confirm ?? '').trim().toLowerCase() !== 'reset') return problem(422, 'type "reset" to confirm');
    const plan = resetPlan();
    state.allow = ALLOWLIST.filter((h) => !h.endsWith('.amazonaws.com'));
    state.creds = state.creds.filter((c) => c.kind === 'builtin');
    for (const c of state.creds) c.op = null;
    db.revoked.clear();
    state.role = DEFAULT_ROLE as RoleId;
    state.sessionPolicy = false;
    state.actions = [];
    state.scopeAt = Date.now();
    state.scopeBy = MOCK_EMAIL;
    const w = write('policy.reset', 'Reset the proxy policy to its defaults', { reactivated: plan.credentials.reactivate }, true);
    return HttpResponse.json({ plan, ...w });
  }),

  http.get('/api/policy/config', async () => {
    await latency();
    if (!IS_ADMIN) return forbidden();
    const p = pending();
    const a = state.applied;
    const yaml = configYaml();
    const scope = JSON.stringify(scopeDoc(), null, 2);
    const scopeVersion = state.scope;
    const cfgAt = iso(state.requestedAt);
    return HttpResponse.json({
      enabled: true,
      server_time: iso(Date.now()),
      bucket: 'my2cents-ip-control-example',
      desired: {
        config: { key: 'iron-proxy.yaml', version: state.config, last_modified: cfgAt, etag: etagOf(yaml), version_id: `${hex(8)}.${hex(20)}`, size: yaml.length, updated_at: cfgAt, updated_by: state.changes[0]?.actor ?? MOCK_EMAIL },
        scope: { key: 'aws-scope.json', version: scopeVersion, last_modified: iso(state.scopeAt), etag: etagOf(scope), version_id: `${hex(8)}.${hex(20)}`, size: scope.length, updated_at: iso(state.scopeAt), updated_by: state.scopeBy, role: state.role, session_policy: state.sessionPolicy, seed: false },
      },
      applied: {
        status_key: 'status.json',
        status_last_modified: iso(lastRound()),
        config: { version: a.config, synced_version: a.config, synced_at: iso(a.at - 400), etag: p ? etagOf(`v${a.config}`) : etagOf(yaml), applied_at: iso(a.at), rejected_version: null, refused_reason: null },
        scope: { version: a.scope, synced_version: a.scope, role: a.role, session_policy: a.sessionPolicy, session_name: `proxy-${a.role}-v${a.scope}`, applied_at: iso(a.at), assumed_at: iso(a.assumedAt), expiration: iso(a.expiration), error: null },
        reload: { at: iso(a.at), ok: a.reloadOk, http_status: a.reloadOk ? 200 : 422, error: a.reloadError, pending: false },
        sidecar: sidecarView(),
        latency_s: p ? null : Math.round(a.latency * 10) / 10,
      },
      sync: {
        state: p ? 'pending' : 'in_sync',
        behind: [a.config < state.config ? 'config' : null, a.scope < scopeVersion ? 'scope' : null].filter(Boolean),
        detail: p
          ? `The bucket has config v${state.config}; the sidecar applies it at its next check (every ${POLL_S} s).`
          : `iron-proxy runs config v${state.config} and AWS scope v${scopeVersion}, the versions in the bucket.`,
        etag_match: !p,
      },
      error: null,
      content: { config: yaml, scope },
      redactions: { config: [], scope: [] },
    });
  }),

  http.get('/api/policy/allowlist', async () => {
    await latency();
    return HttpResponse.json({
      version: state.config,
      hosts: [...state.allow.map((host) => ({ host, managed: null })), ...awsHosts().map((h) => ({ host: h.host, managed: 'aws', service: h.service }))],
    });
  }),

  http.post('/api/policy/allowlist', async ({ request }) => {
    await latency();
    if (!IS_ADMIN) return forbidden();
    const body = (await request.json()) as { host?: string };
    const host = String(body.host ?? '').trim().toLowerCase().replace(/\.$/, '');
    const err = hostError(host);
    if (err) return problem(422, err);
    if (state.allow.includes(host)) return problem(409, `${host} is already on the allowlist`);
    state.allow.push(host);
    const w = write('allowlist.add', `Allowed ${host}`, { host });
    return HttpResponse.json({ version: state.config, host, ...w }, { status: 201 });
  }),

  http.delete('/api/policy/allowlist/:host', async ({ params }) => {
    await latency();
    if (!IS_ADMIN) return forbidden();
    const host = decodeURIComponent(String(params.host));
    if (awsHosts().some((h) => h.host === host)) return problem(409, `${host} follows AWS access; remove the service there`);
    if (!state.allow.includes(host)) return problem(404, `${host} isn’t on the allowlist`);
    state.allow = state.allow.filter((h) => h !== host);
    const w = write('allowlist.remove', `Removed ${host}`, { host });
    return HttpResponse.json({ version: state.config, host, ...w });
  }),

  http.get('/api/policy/credentials', async () => {
    await latency();
    return HttpResponse.json(state.creds.map(credView));
  }),

  http.post('/api/policy/credentials', async ({ request }) => {
    await delay(400 + Math.random() * 300);
    if (!IS_ADMIN) return forbidden();
    const b = (await request.json()) as Record<string, unknown>;
    const name = String(b.name ?? '');
    const env = String(b.env ?? '');
    const hosts = Array.isArray(b.hosts) ? b.hosts.map(String) : [];
    const headers = Array.isArray(b.headers) ? b.headers.map(String) : [];
    const value = typeof b.value === 'string' ? b.value : '';
    if (!/^[a-z][a-z0-9-]{1,31}$/.test(name)) return field('name', 'must match ^[a-z][a-z0-9-]{1,31}$');
    if (!/^[A-Z_][A-Z0-9_]{0,63}$/.test(env)) return field('env', 'must match ^[A-Z_][A-Z0-9_]{0,63}$');
    if (!value) return field('value', 'String should have at least 1 character');
    if (state.creds.some((c) => c.name === name)) return problem(409, `a credential named ${name} already exists`);
    if (state.creds.some((c) => c.env === env) || ['PATH', 'HOME', 'HTTPS_PROXY', 'SANDBOX_NAME'].includes(env) || env.startsWith('AWS_')) {
      return problem(409, `${env} is reserved or used by another credential`);
    }
    for (const h of hosts) {
      const e = hostError(h);
      if (e) return problem(422, `${h}: ${e}`);
    }
    const placeholder = `m2c-ph-${name}-${hex(16)}`;
    if (payloadBytes(env.length + placeholder.length + 6) > 4096) {
      return problem(409, 'a new sandbox’s run hook payload would pass 4,096 bytes with this credential; delete one first');
    }
    const c: MCred = { name, kind: 'custom', env, placeholder, hosts, headers, query: b.query === true, body: b.body === true, created_at: Date.now(), created_by: MOCK_EMAIL, rotated_at: null, rotated_by: null, op: null };
    state.creds.push(c);
    const view = credView(c);
    const warnings = view.on_allowlist ? [] : hosts.filter((h) => !currentAllowlist().includes(h)).map((h) => `${h} isn’t on the allowlist: iron-proxy refuses it before it could swap the placeholder.`);
    const w = write('credential.create', `Added credential ${name} (${env}) for ${hosts.join(', ')}`, { name, env, hosts, headers, query: c.query, body: c.body });
    return HttpResponse.json({ credential: view, warnings, ...w }, { status: 201 });
  }),

  http.post('/api/policy/credentials/:name/rotate', async ({ params, request }) => {
    await delay(300 + Math.random() * 200);
    if (!IS_ADMIN) return forbidden();
    const c = state.creds.find((x) => x.name === params.name);
    if (!c) return problem(404, 'unknown credential');
    if (c.kind !== 'custom') return problem(409, `${c.name} is built in: rotate it from part 2's .env`);
    const b = (await request.json()) as { value?: string };
    if (!b.value) return field('value', 'String should have at least 1 character');
    c.rotated_at = Date.now();
    c.rotated_by = MOCK_EMAIL;
    const change: MChange = { id: `${new Date().toISOString()}#${hex(6)}`, at: Date.now(), actor: MOCK_EMAIL, kind: 'credential.rotate', summary: `Rotated ${c.name}`, detail: { name: c.name }, config_version: state.config, scope_version: state.scope };
    state.changes.unshift(change);
    return HttpResponse.json({ name: c.name, rotated_at: iso(c.rotated_at), effective_within_s: 30, change });
  }),

  http.delete('/api/policy/credentials/:name', async ({ params }) => {
    await latency();
    if (!IS_ADMIN) return forbidden();
    const c = state.creds.find((x) => x.name === params.name);
    if (!c) return problem(404, 'unknown credential');
    if (c.kind !== 'custom') return problem(409, `${c.name} is built in and can't be deleted here`);
    state.creds = state.creds.filter((x) => x !== c);
    db.revoked.delete(c.name);
    const w = write('credential.delete', `Deleted credential ${c.name}`, { name: c.name });
    return HttpResponse.json({ name: c.name, deleted: true, recovery_window_days: 7, ...w });
  }),

  ...(['revoke', 'restore'] as const).map((op) =>
    http.post(`/api/credentials/:name/${op}`, async ({ params }) => {
      await delay(300 + Math.random() * 200);
      if (!IS_ADMIN) return forbidden();
      const c = state.creds.find((x) => x.name === params.name);
      if (!c) return problem(404, 'unknown credential');
      const w = write(`credential.${op}`, `${op === 'revoke' ? 'Revoked' : 'Restored'} ${c.name}`, { name: c.name });
      c.op = { op, config: state.config, since: Date.now() };
      return HttpResponse.json({
        name: c.name,
        state: op === 'revoke' ? 'revoking' : 'restoring',
        revoked: op === 'revoke' ? 'pending' : false,
        method: 'reload',
        effective_after_s: 10,
        since: iso(Date.now()),
        detail: op === 'revoke' ? 'Deny policy in place; iron-proxy reloads without the cached key.' : 'Deny policy removed; iron-proxy reloads and fetches the key again.',
        warning: `${op === 'revoke' ? 'Revoked' : 'Restored'} for every sandbox on this proxy (mock).`,
        ...w,
      });
    }),
  ),

  http.get('/api/policy/aws/catalog', async ({ request }) => {
    await latency();
    const role = roleOf(new URL(request.url).searchParams.get('role'));
    if (!role) return problem(422, 'role: one of view-only, read-only, data-reader, builder');
    const fetched = t0 - 13 * 60_000;
    const age = Math.round((Date.now() - fetched) / 1000);
    return HttpResponse.json(
      {
        region: REGION,
        source: { url: 'https://servicereference.us-east-1.amazonaws.com/', fetched_at: iso(fetched), age_s: age, stale: false, error: null, services: SERVICES.length, actions: TOTAL_ACTIONS, fetch_s: 4.1 },
        role,
        roles: ROLES.map(({ id, description, default: d, warning }) => ({ id, description, default: d, warning })),
        ceiling: { role, policies: ROLES.find((r) => r.id === role)!.policies, fetched_at: iso(fetched), age_s: age, stale: false },
        levels: ['List', 'Read', 'Write', 'Permissions', 'Tagging'],
        services: SERVICES.map((sv) => summary(sv, role)),
        baseline: BASELINE,
        limits: { policy_chars: 2048 },
      },
      { headers: { 'Cache-Control': 'private, max-age=3600', 'X-Catalog-Age': String(age) } },
    );
  }),

  http.get('/api/policy/aws/services/:id', async ({ params, request }) => {
    await latency();
    const role = roleOf(new URL(request.url).searchParams.get('role'));
    if (!role) return problem(422, 'role: one of view-only, read-only, data-reader, builder');
    const s = BY_ID.get(String(params.id));
    if (!s) return problem(404, 'unknown service');
    return HttpResponse.json(
      {
        id: s.id,
        name: s.name,
        enabled: s.enabled,
        reason: s.reason,
        signing_names: s.signing,
        hosts: s.hosts,
        actions: actionsFor(s, role),
        wildcards: wildcards(s, role).map(({ covered, ...w }) => w),
      },
      { headers: { 'Cache-Control': 'private, max-age=3600' } },
    );
  }),

  http.get('/api/policy/aws/search', async ({ request }) => {
    await delay(40 + Math.random() * 90);
    const u = new URL(request.url).searchParams;
    const lv = u.get('level');
    const level = lv && ['List', 'Read', 'Write', 'Permissions', 'Tagging'].includes(lv) ? (lv as Level) : null;
    const limit = Math.max(1, Math.min(500, Number(u.get('limit') ?? 100) || 100));
    const role = roleOf(u.get('role'));
    if (!role) return problem(422, 'role: one of view-only, read-only, data-reader, builder');
    return HttpResponse.json(search(u.get('q') ?? '', level, limit, role), { headers: { 'Cache-Control': 'private, max-age=3600' } });
  }),

  http.get('/api/policy/aws/scope', async () => {
    await latency();
    const p = current();
    return HttpResponse.json({
      version: state.scope,
      role: state.role,
      session_policy: state.sessionPolicy,
      actions: state.actions,
      services: [...new Set(state.actions.map((a) => a.split(':')[0]))].sort(),
      updated_at: iso(state.scopeAt),
      updated_by: state.scopeBy,
      preview: 'error' in p ? null : p,
    });
  }),

  http.post('/api/policy/aws/preview', async ({ request }) => {
    await delay(40 + Math.random() * 80);
    const b = (await request.json()) as { role?: string; session_policy?: boolean; actions?: string[] };
    const role = roleOf(b.role);
    if (!role) return problem(422, `role: unknown role ${b.role}`);
    const p = preview(role, b.session_policy === true, Array.isArray(b.actions) ? b.actions.map(String) : []);
    if ('error' in p) return problem(422, p.error!);
    return HttpResponse.json(p);
  }),

  http.put('/api/policy/aws/scope', async ({ request }) => {
    await delay(250 + Math.random() * 200);
    if (!IS_ADMIN) return forbidden();
    const b = (await request.json()) as { role?: string; session_policy?: boolean; actions?: string[] };
    const role = roleOf(b.role);
    if (!role) return problem(422, `role: unknown role ${b.role}`);
    const sp = b.session_policy === true;
    const p = preview(role, sp, Array.isArray(b.actions) ? b.actions.map(String) : []);
    if ('error' in p) return problem(422, p.error!);
    if (!p.fits) return problem(422, `the session policy is ${p.chars} characters; STS allows 2,048`);
    if (role === state.role && sp === state.sessionPolicy && p.actions.join() === [...state.actions].sort().join()) {
      return HttpResponse.json({ scope_version: state.scope, config_version: state.config, preview: p, change: null, apply: null, unchanged: true });
    }
    state.actions = p.actions;
    state.role = role;
    state.sessionPolicy = sp;
    state.scopeAt = Date.now();
    state.scopeBy = MOCK_EMAIL;
    const what = sp ? `${p.actions.length} action${p.actions.length === 1 ? '' : 's'} on ${p.services.length ? p.services.join(', ') : 'none'}` : 'no session policy';
    const w = write('aws.scope', `AWS access: role ${role}, ${what}`, { role, session_policy: sp, actions: p.actions }, true);
    return HttpResponse.json({ scope_version: state.scope, config_version: state.config, preview: p, unchanged: false, ...w });
  }),

  http.get('/api/policy/changes', async ({ request }) => {
    await latency();
    const limit = Math.min(200, Number(new URL(request.url).searchParams.get('limit') ?? 50) || 50);
    return HttpResponse.json(state.changes.slice(0, limit));
  }),
];
