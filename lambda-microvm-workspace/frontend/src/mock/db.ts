// In-memory simulation of the backend for VITE_MOCK=1. Every value here is synthetic.

export type MState = 'PENDING' | 'RUNNING' | 'SUSPENDING' | 'SUSPENDED' | 'TERMINATING' | 'TERMINATED';

export interface MSandbox {
  id: string;
  name: string;
  state: MState;
  created_at: string;
  started_at: string;
  max_duration_s: number;
  idle_minutes: number;
  size: MSize;
  auto_resume: boolean;
  /** null: until the lifetime ends */
  suspended_duration_s: number | null;
  env_names: string[];
  image_version: string;
  lastActivity: number;
  attached: number;
  terminatedAt?: number;
  cpu: number;
  mem: number;
}

export interface MProxyRecord {
  ts: number;
  action: 'allow' | 'reject';
  status: number;
  method: string;
  host: string;
  path: string;
  rejected_by: string | null;
  secrets: string[];
  aws?: { service: string; region: string; signed: boolean; rejected: string | null } | null;
}

/** What the proxy policy mock (mock/policy.ts) decides; defaults keep the old behaviour. */
export const gates = {
  allowed: (host: string) => ALLOWLIST.includes(host),
  swappable: (_name: string) => true,
  awsService: (_signingName: string) => true,
};

export interface MSession {
  session_id: string;
  project: string;
  model: string;
  turns: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_creation_tokens: number;
  first_ts: string;
  last_ts: string;
}

const scenario = new URLSearchParams(window.location.search).get('mock') ?? '';
export const MOCK_SCENARIO = scenario;

const now = Date.now();
const iso = (ms: number) => new Date(ms).toISOString();
const rid = () => Math.random().toString(36).slice(2, 10);

export const QUOTA_GB = 8;
export const GB_PER_SANDBOX = 2;
export const MAX_DURATION_S = 8 * 3600;

export type MSize = 'small' | 'medium' | 'large' | 'xlarge';
/** Same sizes and prices as the backend (app/sizes.py, app/pricing.py). */
export const SIZES: Record<MSize, { label: string; gb: number; vcpu: number; usd: number }> = {
  small: { label: 'Small', gb: 1, vcpu: 0.5, usd: 0.0631 },
  medium: { label: 'Medium', gb: 2, vcpu: 1, usd: 0.1261 },
  large: { label: 'Large', gb: 4, vcpu: 2, usd: 0.2522 },
  xlarge: { label: 'X-Large', gb: 8, vcpu: 4, usd: 0.5044 },
};

// Pricing per million tokens, used only by the mock to fake est_cost_usd.
const PRICES: Record<string, { in: number; out: number; cr: number; cw: number }> = {
  'claude-sonnet-4-5': { in: 3, out: 15, cr: 0.3, cw: 3.75 },
  'claude-opus-4-5': { in: 5, out: 25, cr: 0.5, cw: 6.25 },
  'claude-haiku-4-5': { in: 1, out: 5, cr: 0.1, cw: 1.25 },
};

function seedSandboxes(): MSandbox[] {
  if (scenario === 'empty') return [];
  const mk = (name: string, state: MState, ageMin: number, idleMin: number, idleUsedMin: number, size: MSize = 'medium'): MSandbox => ({
    id: `mvm-${rid()}${rid()}`.slice(0, 20),
    name,
    state,
    created_at: iso(now - ageMin * 60_000 - 2000),
    started_at: iso(now - ageMin * 60_000),
    max_duration_s: MAX_DURATION_S,
    idle_minutes: idleMin,
    size,
    auto_resume: name !== 'docs-site',
    suspended_duration_s: name === 'docs-site' ? 3600 : null,
    env_names: name === 'refactor-auth' ? ['NODE_ENV', 'APP_REGION'] : [],
    image_version: 'v2.3',
    lastActivity: now - idleUsedMin * 60_000,
    attached: 0,
    cpu: 4 + Math.random() * 6,
    mem: 620 + Math.random() * 200,
  });
  const full = scenario === 'full';
  const list = [
    mk('refactor-auth', 'RUNNING', 72, 15, 0, 'medium'),
    mk('docs-site', 'SUSPENDED', 341, 15, 0, 'small'),
    mk('flaky-tests', 'RUNNING', 452, 10, 9.4, full ? 'small' : 'medium'),
  ];
  if (full) list.push(mk('perf-bench', 'RUNNING', 20, 30, 2, 'large'));
  return list;
}

export const db = {
  sandboxes: seedSandboxes(),
  proxy: [] as MProxyRecord[],
  revoked: new Set<string>(),
  usage: {} as Record<string, MSession[]>,
};

export const PLACEHOLDERS: Record<string, string> = {
  anthropic: 'sk-ant-api03-PLACEHOLDER',
  github: 'ghp_PLACEHOLDER',
  demo: 'demo_PLACEHOLDER',
};

export const ALLOWLIST = [
  'api.anthropic.com',
  'statsig.anthropic.com',
  'api.github.com',
  'github.com',
  'codeload.github.com',
  'objects.githubusercontent.com',
  'registry.npmjs.org',
  'pypi.org',
  'files.pythonhosted.org',
  'httpbin.org',
];

// ---------- event bus to fake /ws/events subscribers ----------

type Sub = (msg: Record<string, unknown>) => void;
const subs = new Set<Sub>();
export function subscribeEvents(fn: Sub) {
  subs.add(fn);
  return () => subs.delete(fn);
}
function emit(msg: Record<string, unknown>) {
  subs.forEach((fn) => fn(msg));
}

type StateSub = (sb: MSandbox, prev: MState) => void;
const stateSubs = new Set<StateSub>();
export function onStateChange(fn: StateSub) {
  stateSubs.add(fn);
  return () => stateSubs.delete(fn);
}

export function setState(sb: MSandbox, to: MState) {
  const prev = sb.state;
  if (prev === to) return;
  sb.state = to;
  if (to === 'RUNNING') sb.lastActivity = Date.now();
  if (to === 'TERMINATED') sb.terminatedAt = Date.now();
  emit({ type: 'lifecycle', id: sb.id, name: sb.name, state: to, prev, ts: new Date().toISOString() });
  stateSubs.forEach((fn) => fn(sb, prev));
}

export function transition(sb: MSandbox, via: MState, to: MState, ms: number) {
  setState(sb, via);
  window.setTimeout(() => {
    if (sb.state === via) setState(sb, to);
  }, ms);
}

export function findSandbox(id: string) {
  return db.sandboxes.find((s) => s.id === id);
}

export function usedGb(): number {
  return db.sandboxes.filter((s) => s.state !== 'TERMINATED').reduce((n, s) => n + SIZES[s.size].gb, 0);
}

export interface MNew {
  name: string;
  size: MSize;
  idleMinutes: number;
  maxMinutes: number;
  suspendedMinutes: number | null;
  autoResume: boolean;
  envNames: string[];
}

export function createSandbox(o: MNew): MSandbox {
  const t = Date.now();
  const sb: MSandbox = {
    id: `mvm-${rid()}${rid()}`.slice(0, 20),
    name: o.name,
    state: 'PENDING',
    created_at: iso(t),
    started_at: iso(t),
    max_duration_s: Math.min(MAX_DURATION_S, o.maxMinutes * 60),
    idle_minutes: o.idleMinutes,
    size: o.size,
    auto_resume: o.autoResume,
    suspended_duration_s: o.suspendedMinutes === null ? null : o.suspendedMinutes * 60,
    env_names: o.envNames,
    image_version: 'v2.3',
    lastActivity: t,
    attached: 0,
    cpu: 30,
    mem: 540,
  };
  db.sandboxes.push(sb);
  emit({ type: 'lifecycle', id: sb.id, name: sb.name, state: 'PENDING', prev: null, ts: new Date().toISOString() });
  window.setTimeout(() => setState(sb, 'RUNNING'), 2000);
  return sb;
}

// ---------- proxy + DNS traffic ----------

const lastDns = new Map<string, number>();

type ProxyInput = Omit<MProxyRecord, 'ts' | 'action' | 'status' | 'rejected_by' | 'aws'> & {
  status?: number;
  aws?: { service: string; region: string };
};

/** What iron-proxy would log for this request under the current (mock) policy. */
function decide(r: ProxyInput, ts: number): MProxyRecord {
  const allowed = gates.allowed(r.host);
  // A revoked key is never swapped in: the request leaves with the placeholder and fails upstream.
  const live = r.secrets.filter((s) => gates.swappable(s));
  const swapped = allowed ? live.filter((s) => !db.revoked.has(s)) : [];
  const lostKey = allowed && live.some((s) => db.revoked.has(s));
  // aws_auth: region and service gates, then a re-signature with the scoped role.
  let aws: MProxyRecord['aws'] = null;
  let rejectedBy: string | null = allowed ? null : 'allowlist';
  if (allowed && r.aws) {
    const rejected =
      r.aws.region !== 'us-east-1' ? 'region_not_allowed' : !gates.awsService(r.aws.service) ? 'service_not_allowed' : null;
    aws = { service: r.aws.service, region: r.aws.region, signed: !rejected, rejected };
    if (rejected) rejectedBy = 'aws_auth';
  }
  const rec: MProxyRecord = {
    ts,
    action: rejectedBy ? 'reject' : 'allow',
    status: rejectedBy ? 403 : lostKey ? 401 : (r.status ?? 200),
    method: r.method,
    host: r.host,
    path: r.path,
    rejected_by: rejectedBy,
    secrets: swapped,
    aws,
  };
  return rec;
}

export function proxyRequest(r: ProxyInput) {
  const ts = Date.now();
  const rec = decide(r, ts);
  dnsLookup(r.host);
  db.proxy.push(rec);
  if (db.proxy.length > 4000) db.proxy.splice(0, 1000);
  emit({ type: 'proxy', ...rec, ts: new Date(ts).toISOString() });
  return rec;
}

export function dnsLookup(name: string, rcode = 'NOERROR') {
  const t = Date.now();
  if (rcode === 'NOERROR' && t - (lastDns.get(name) ?? 0) < 45_000) return;
  lastDns.set(name, t);
  const src = `10.50.${20 + Math.floor(Math.random() * 2)}.${10 + Math.floor(Math.random() * 200)}`;
  emit({ type: 'dns', ts: new Date(t).toISOString(), query: `${name}.`, qtype: 'A', rcode, srcaddr: src });
  if (rcode === 'NOERROR' && Math.random() < 0.5) {
    window.setTimeout(
      () => emit({ type: 'dns', ts: new Date().toISOString(), query: `${name}.`, qtype: 'AAAA', rcode, srcaddr: src }),
      8,
    );
  }
}

interface Pattern {
  w: number;
  method: string;
  host: string;
  paths: string[];
  secrets: string[];
  status?: number;
  aws?: { service: string; region: string };
}

const BACKGROUND: Pattern[] = [
  { w: 9, method: 'POST', host: 'api.anthropic.com', paths: ['/v1/messages?beta=true'], secrets: ['anthropic'] },
  { w: 3, method: 'POST', host: 'statsig.anthropic.com', paths: ['/v1/rgstr'], secrets: [] },
  { w: 4, method: 'GET', host: 'api.github.com', paths: ['/repos/my2cents/blog/pulls?state=open', '/user', '/repos/my2cents/blog/commits/main/status'], secrets: ['github'] },
  { w: 2, method: 'GET', host: 'github.com', paths: ['/my2cents/blog.git/info/refs?service=git-upload-pack'], secrets: ['github'] },
  { w: 4, method: 'GET', host: 'registry.npmjs.org', paths: ['/@anthropic-ai%2fclaude-code', '/typescript', '/vite', '/@types%2fnode'], secrets: [] },
  { w: 2, method: 'GET', host: 'pypi.org', paths: ['/simple/boto3/', '/simple/fastapi/'], secrets: [] },
  { w: 2, method: 'GET', host: 'files.pythonhosted.org', paths: ['/packages/7c/ab/boto3-1.43.123-py3-none-any.whl'], secrets: [] },
  { w: 1, method: 'POST', host: 'httpbin.org', paths: ['/anything/demo'], secrets: ['demo'] },
  { w: 1, method: 'POST', host: 'sentry.io', paths: ['/api/4504/envelope/'], secrets: [] },
  { w: 1, method: 'GET', host: 'raw.githubusercontent.com', paths: ['/nvm-sh/nvm/v0.40.1/install.sh'], secrets: [] },
  { w: 0.5, method: 'POST', host: 'pastebin.com', paths: ['/api/api_post.php'], secrets: [] },
  { w: 1, method: 'GET', host: 'httpbin.org', paths: ['/headers'], secrets: ['httpbin-test'] },
  { w: 1.4, method: 'POST', host: 'sts.us-east-1.amazonaws.com', paths: ['/'], secrets: [], aws: { service: 'sts', region: 'us-east-1' } },
  { w: 1, method: 'GET', host: 's3.us-east-1.amazonaws.com', paths: ['/', '/?list-type=2&prefix=reports%2F'], secrets: [], aws: { service: 's3', region: 'us-east-1' } },
  { w: 0.6, method: 'POST', host: 'ecs.us-east-1.amazonaws.com', paths: ['/'], secrets: [], aws: { service: 'ecs', region: 'us-east-1' } },
  { w: 0.3, method: 'GET', host: 's3.amazonaws.com', paths: ['/'], secrets: [], aws: { service: 's3', region: 'eu-west-1' } },
  { w: 0.3, method: 'POST', host: 'lambda.us-east-1.amazonaws.com', paths: ['/2015-03-31/functions/'], secrets: [], aws: { service: 'lambda', region: 'us-east-1' } },
];
const TOTAL_W = BACKGROUND.reduce((a, p) => a + p.w, 0);

function pickPattern(): Pattern {
  let r = Math.random() * TOTAL_W;
  for (const p of BACKGROUND) {
    r -= p.w;
    if (r <= 0) return p;
  }
  return BACKGROUND[0];
}

function backgroundRequest() {
  const p = pickPattern();
  proxyRequest({
    method: p.method,
    host: p.host,
    path: p.paths[Math.floor(Math.random() * p.paths.length)],
    secrets: p.secrets,
    status: p.status,
    aws: p.aws,
  });
}

const NX = ['wpad.ec2.internal', 'metadata.google.internal', 'telemetry.invalid', 'api.anthropic.co', 'registry.npmjs.or'];
const AWS = ['sts.us-east-1.amazonaws.com', 'logs.us-east-1.amazonaws.com', 'ecr.us-east-1.amazonaws.com'];

/** Seeds 15 minutes of history into the proxy log (credentials panel) only; events start live. */
function seedHistory() {
  if (scenario === 'empty') return;
  const t0 = Date.now() - 15 * 60_000;
  for (let t = t0; t < Date.now(); t += 2000 + Math.random() * 9000) {
    const p = pickPattern();
    db.proxy.push(decide({ method: p.method, host: p.host, path: p.paths[0], secrets: p.secrets, status: p.status, aws: p.aws }, t));
  }
}

/** On connect, replay a short burst of recent traffic so the panels are not blank in demos. */
export function replayRecent(send: Sub) {
  if (scenario === 'empty') return;
  const recent = db.proxy.slice(-60);
  for (const r of recent) send({ type: 'proxy', ...r, ts: new Date(r.ts).toISOString() });
  const names = [...new Set(recent.map((r) => r.host))];
  let t = Date.now() - 6 * 60_000;
  for (const n of names) {
    t += 20_000;
    send({ type: 'dns', ts: new Date(t).toISOString(), query: `${n}.`, qtype: 'A', rcode: 'NOERROR', srcaddr: '10.50.20.41' });
  }
  send({ type: 'dns', ts: new Date(t + 5000).toISOString(), query: 'wpad.ec2.internal.', qtype: 'A', rcode: 'NXDOMAIN', srcaddr: '10.50.20.41' });
  send({ type: 'dns', ts: new Date(t + 9000).toISOString(), query: 'sts.us-east-1.amazonaws.com.', qtype: 'A', rcode: 'NOERROR', srcaddr: '10.50.21.77' });
  // The workspace backend's own lookups come from the proxy subnets, not the sandbox subnets.
  send({ type: 'dns', ts: new Date(t + 11000).toISOString(), query: 'logs.us-east-1.amazonaws.com.', qtype: 'A', rcode: 'NOERROR', srcaddr: '10.50.11.24' });
}

// ---------- vitals + usage ----------

export function stats(sb: MSandbox) {
  sb.cpu = Math.max(1, Math.min(97, sb.cpu + (Math.random() - 0.5) * 14 + (sb.attached ? 2 : -1)));
  sb.mem = Math.max(380, Math.min(2900, sb.mem + (Math.random() - 0.45) * 60));
  const uptime = (Date.now() - Date.parse(sb.started_at)) / 1000;
  return {
    cpu_pct: Math.round(sb.cpu * 10) / 10,
    cpu_count: 2,
    mem_total_mb: 7936,
    mem_used_mb: Math.round(sb.mem),
    load1: Math.round((sb.cpu / 50) * 100) / 100,
    disk_used_mb: 3420,
    disk_total_mb: 10240,
    uptime_s: Math.round(uptime),
    procs: 41 + Math.floor(Math.random() * 6),
  };
}

function costOf(s: MSession) {
  const p = PRICES[s.model] ?? PRICES['claude-sonnet-4-5'];
  return (
    (s.input_tokens * p.in + s.output_tokens * p.out + s.cache_read_tokens * p.cr + s.cache_creation_tokens * p.cw) /
    1_000_000
  );
}

export function usageFor(id: string) {
  const sessions = (db.usage[id] ?? []).map((s) => ({ ...s, est_cost_usd: Math.round(costOf(s) * 10000) / 10000, priced_as: s.model }));
  const sum = (k: keyof MSession) => sessions.reduce((a, s) => a + (s[k] as number), 0);
  return {
    sessions,
    totals: {
      turns: sum('turns'),
      input_tokens: sum('input_tokens'),
      output_tokens: sum('output_tokens'),
      cache_read_tokens: sum('cache_read_tokens'),
      cache_creation_tokens: sum('cache_creation_tokens'),
      est_cost_usd: Math.round(sessions.reduce((a, s) => a + s.est_cost_usd, 0) * 10000) / 10000,
    },
    unpriced_models: [],
    pricing: { source: 'Anthropic', as_of: '2026-11-01', note: 'Estimated from Anthropic list prices as of 2026-11-01 (mock)' },
  };
}

export function startClaudeSession(id: string): MSession {
  const t = new Date().toISOString();
  const s: MSession = {
    session_id: crypto.randomUUID(),
    project: '/root/work',
    model: 'claude-sonnet-4-5',
    turns: 0,
    input_tokens: 0,
    output_tokens: 0,
    cache_read_tokens: 0,
    cache_creation_tokens: 0,
    first_ts: t,
    last_ts: t,
  };
  db.usage[id] = [...(db.usage[id] ?? []), s];
  return s;
}

export function recordTurn(s: MSession) {
  s.turns += 1;
  s.input_tokens += 40 + Math.floor(Math.random() * 400);
  s.output_tokens += 300 + Math.floor(Math.random() * 1400);
  s.cache_read_tokens += 14_000 + Math.floor(Math.random() * 22_000);
  s.cache_creation_tokens += Math.floor(Math.random() * 3000);
  s.last_ts = new Date().toISOString();
}

function seedUsage() {
  const first = db.sandboxes[0];
  if (!first) return;
  const a = startClaudeSession(first.id);
  a.model = 'claude-opus-4-5';
  for (let i = 0; i < 14; i++) recordTurn(a);
  a.first_ts = new Date(Date.now() - 64 * 60_000).toISOString();
  a.last_ts = new Date(Date.now() - 31 * 60_000).toISOString();
  const b = startClaudeSession(first.id);
  for (let i = 0; i < 6; i++) recordTurn(b);
  b.first_ts = new Date(Date.now() - 22 * 60_000).toISOString();
  b.last_ts = new Date(Date.now() - 3 * 60_000).toISOString();
  const third = db.sandboxes[2];
  if (third) {
    const c = startClaudeSession(third.id);
    c.model = 'claude-haiku-4-5';
    for (let i = 0; i < 3; i++) recordTurn(c);
  }
}

// ---------- the simulation clock ----------

let started = false;
export function startSimulation() {
  if (started) return;
  started = true;
  seedHistory();
  seedUsage();
  const loop = () => {
    if (db.sandboxes.some((s) => s.state === 'RUNNING')) backgroundRequest();
    window.setTimeout(loop, 700 + Math.random() * 2600);
  };
  window.setTimeout(loop, 1500);
  window.setInterval(() => {
    const r = Math.random();
    if (r < 0.25) dnsLookup(NX[Math.floor(Math.random() * NX.length)], 'NXDOMAIN');
    else if (r < 0.5) dnsLookup(AWS[Math.floor(Math.random() * AWS.length)]);
  }, 9000);
  window.setInterval(() => {
    const t = Date.now();
    for (const sb of db.sandboxes) {
      if (sb.state === 'RUNNING' && sb.attached === 0 && t - sb.lastActivity > sb.idle_minutes * 60_000) {
        transition(sb, 'SUSPENDING', 'SUSPENDED', 500);
      }
      if (sb.state !== 'TERMINATED' && sb.state !== 'TERMINATING' && t - Date.parse(sb.started_at) > sb.max_duration_s * 1000) {
        transition(sb, 'TERMINATING', 'TERMINATED', 1500);
      }
    }
    // Keep terminated sandboxes flagged for an hour, like the backend.
    db.sandboxes = db.sandboxes.filter((s) => !s.terminatedAt || t - s.terminatedAt < 3_600_000);
  }, 1000);
}
