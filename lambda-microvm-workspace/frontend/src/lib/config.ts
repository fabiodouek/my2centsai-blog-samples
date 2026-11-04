export interface RuntimeConfig {
  region: string;
  userPoolId: string;
  clientId: string;
  cognitoDomain: string;
  apiBase: string;
}

export const MOCK = import.meta.env.VITE_MOCK === '1';

let runtime: RuntimeConfig | null = null;

export class ConfigError extends Error {}

/** Loads /config.json, which the deploy script writes next to index.html. */
export async function loadRuntimeConfig(): Promise<RuntimeConfig> {
  if (runtime) return runtime;
  if (MOCK) {
    runtime = {
      region: 'us-east-1',
      userPoolId: 'us-east-1_MOCK',
      clientId: 'mock-client',
      cognitoDomain: 'mock.auth.us-east-1.amazoncognito.com',
      apiBase: '',
    };
    return runtime;
  }
  let res: Response;
  try {
    res = await fetch('/config.json', { cache: 'no-store' });
  } catch {
    throw new ConfigError('Could not fetch /config.json. Check that the site is deployed with its config file.');
  }
  if (!res.ok) {
    throw new ConfigError(
      `/config.json returned ${res.status}. The deploy script writes it; for local dev copy config.example.json to public/config.json.`,
    );
  }
  let raw: Partial<RuntimeConfig>;
  try {
    raw = (await res.json()) as Partial<RuntimeConfig>;
  } catch {
    throw new ConfigError('/config.json is not valid JSON.');
  }
  const missing = (['region', 'userPoolId', 'clientId', 'cognitoDomain'] as const).filter((k) => !raw[k]);
  if (missing.length) throw new ConfigError(`/config.json is missing ${missing.join(', ')}.`);
  runtime = {
    region: raw.region!,
    userPoolId: raw.userPoolId!,
    clientId: raw.clientId!,
    cognitoDomain: raw.cognitoDomain!,
    apiBase: (raw.apiBase ?? '').replace(/\/+$/, ''),
  };
  return runtime;
}

export function getRuntimeConfig(): RuntimeConfig {
  if (!runtime) throw new Error('runtime config not loaded');
  return runtime;
}

export function apiUrl(path: string): string {
  return `${getRuntimeConfig().apiBase}${path}`;
}

export function wsUrl(path: string): string {
  const base = getRuntimeConfig().apiBase;
  if (base) return `${base.replace(/^http/, 'ws')}${path}`;
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${window.location.host}${path}`;
}

/** Cognito domain as a full origin, whether config.json holds a bare host or a URL. */
export function cognitoOrigin(domain: string): string {
  const d = domain.replace(/\/+$/, '');
  return /^https?:\/\//.test(d) ? d : `https://${d}`;
}
