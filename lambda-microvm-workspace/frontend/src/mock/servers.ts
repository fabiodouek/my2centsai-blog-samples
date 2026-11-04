import {
  ALLOWLIST,
  PLACEHOLDERS,
  db,
  findSandbox,
  onStateChange,
  proxyRequest,
  recordTurn,
  replayRecent,
  setState,
  startClaudeSession,
  subscribeEvents,
  type MSandbox,
  type MSession,
  type MState,
} from './db';
import type { FakeServer, FakeSocket } from './fakeSocket';

const enc = new TextEncoder();
const dec = new TextDecoder();

const C = {
  reset: '\x1b[0m',
  dim: '\x1b[2m',
  bold: '\x1b[1m',
  green: '\x1b[1;32m',
  blue: '\x1b[1;34m',
  red: '\x1b[31m',
  yellow: '\x1b[33m',
  claude: '\x1b[38;2;215;119;87m',
  gray: '\x1b[38;5;245m',
};

// ---------------------------------------------------------------- /ws/events

export class EventsServer implements FakeServer {
  private off: (() => void) | null = null;
  private authTimer: number;
  constructor(private sock: FakeSocket) {
    this.authTimer = window.setTimeout(() => sock.serverClose(4401, 'auth timeout'), 5000);
  }
  // Like the backend: the "tail" is idle until this page says it shows a feed panel, starts (about a
  // second of backfill here), and goes idle again 60 s after the panel goes away. The fake events keep
  // flowing either way.
  private feedTimer: number | null = null;
  private source = 'idle';
  private setSource(source: string) {
    this.source = source;
    this.sock.serverSend(
      JSON.stringify({ type: 'status', source, log_groups: ['/my2cents/iron-proxy', '/my2cents/dns-queries'], missing_log_groups: [] }),
    );
  }
  onMessage(data: string | Uint8Array) {
    if (typeof data !== 'string') return;
    let m: { type?: string; on?: unknown } = {};
    try {
      m = JSON.parse(data);
    } catch {
      /* ignore */
    }
    if (m.type === 'ping') {
      this.sock.serverSend(JSON.stringify({ type: 'pong' }));
      return;
    }
    if (m.type === 'feeds' && this.off) {
      if (this.feedTimer) window.clearTimeout(this.feedTimer);
      this.feedTimer = null;
      if (m.on === true && this.source === 'idle') {
        this.setSource('starting');
        this.feedTimer = window.setTimeout(() => this.setSource('live_tail'), 1200);
      } else if (m.on !== true && this.source !== 'idle') {
        this.feedTimer = window.setTimeout(() => this.setSource('idle'), 60_000);
      }
      return;
    }
    if (m.type !== 'auth' || this.off) return;
    window.clearTimeout(this.authTimer);
    const send = (msg: Record<string, unknown>) => this.sock.serverSend(JSON.stringify(msg));
    send({ type: 'status', source: this.source, log_groups: ['/my2cents/iron-proxy', '/my2cents/dns-queries'], missing_log_groups: [] });
    replayRecent(send);
    for (const sb of db.sandboxes) {
      send({ type: 'lifecycle', id: sb.id, name: sb.name, state: sb.state, prev: null, initial: true, ts: new Date().toISOString() });
    }
    this.off = subscribeEvents(send);
    this.beat = window.setInterval(() => send({ type: 'heartbeat' }), 25_000);
  }
  private beat: number | null = null;
  onClientClose() {
    window.clearTimeout(this.authTimer);
    if (this.feedTimer) window.clearTimeout(this.feedTimer);
    if (this.beat) window.clearInterval(this.beat);
    this.off?.();
    this.off = null;
  }
}

// ---------------------------------------------------------------- /ws/terminal/{id}

interface Persisted {
  transcript: string;
  mode: 'shell' | 'claude';
  claude: MSession | null;
  greeted: boolean;
}
const persisted = new Map<string, Persisted>();
const live = new Map<string, Set<ShellServer>>();

onStateChange((sb) => {
  const sessions = live.get(sb.id);
  if (!sessions) return;
  for (const s of [...sessions]) s.onSandboxState(sb);
});

export class ShellServer implements FakeServer {
  private rows = 24;
  private cols = 80;
  private attached = false;
  private authed = false;
  private line = '';
  private busy = false;
  private esc = '';
  private authTimer: number;
  private p: Persisted;

  constructor(
    private sock: FakeSocket,
    private id: string,
  ) {
    this.authTimer = window.setTimeout(() => sock.serverClose(4401, 'auth timeout'), 5000);
    this.p = persisted.get(id) ?? { transcript: '', mode: 'shell', claude: null, greeted: false };
    persisted.set(id, this.p);
  }

  private get sb(): MSandbox | undefined {
    return findSandbox(this.id);
  }

  private status(state: string, detail = '', extra: Record<string, unknown> = {}) {
    this.sock.serverSend(JSON.stringify({ type: 'status', state, detail, ...extra }));
  }

  private out(s: string, record = true) {
    if (record) {
      this.p.transcript += s;
      if (this.p.transcript.length > 24_000) this.p.transcript = this.p.transcript.slice(-16_000);
    }
    this.sock.serverSend(enc.encode(s));
  }

  onMessage(data: string | Uint8Array) {
    if (typeof data === 'string') {
      let m: { type?: string; rows?: number; cols?: number } = {};
      try {
        m = JSON.parse(data);
      } catch {
        return;
      }
      if (m.type === 'ping') {
        this.sock.serverSend(JSON.stringify({ type: 'pong' }));
      } else if (m.type === 'auth' && !this.authed) {
        this.authed = true;
        window.clearTimeout(this.authTimer);
        if (!live.has(this.id)) live.set(this.id, new Set());
        live.get(this.id)!.add(this);
        void this.open();
      } else if (m.type === 'resize' && m.rows && m.cols) {
        const changed = m.rows !== this.rows || m.cols !== this.cols;
        this.rows = m.rows;
        this.cols = m.cols;
        if (this.attached && changed) {
          // tmux repaints the whole screen on resize; do the same so no stale status line is left behind.
          this.out('\x1b[r\x1b[H\x1b[2J', false);
          this.out(this.p.transcript.slice(-6000), false);
          this.drawStatusBar();
        }
      }
      return;
    }
    if (!this.attached) return;
    this.input(dec.decode(data));
  }

  onClientClose() {
    window.clearTimeout(this.authTimer);
    if (this.beat) window.clearInterval(this.beat);
    live.get(this.id)?.delete(this);
    this.detach();
  }

  private detach() {
    if (!this.attached) return;
    this.attached = false;
    const sb = this.sb;
    if (sb) {
      sb.attached = Math.max(0, sb.attached - 1);
      sb.lastActivity = Date.now();
    }
  }

  private waitingForResume = false;

  onSandboxState(sb: MSandbox) {
    if (sb.state === 'SUSPENDING' || sb.state === 'SUSPENDED') {
      if (this.waitingForResume) return;
      // Like the backend: report suspended, keep the browser socket, reattach when RUNNING again.
      this.status('suspended', 'The sandbox is suspended. Its shell connection closed; resume to reattach.', { vm_state: sb.state });
      this.detach();
      this.waitingForResume = true;
    } else if (sb.state === 'RUNNING' && this.waitingForResume) {
      this.waitingForResume = false;
      this.status('connecting', 'Sandbox resumed. Reattaching tmux');
      window.setTimeout(() => {
        if (this.sock.readyState === 1 && sb.state === 'RUNNING') this.attach(sb);
      }, 500);
    } else if (sb.state === 'TERMINATING' || sb.state === 'TERMINATED') {
      this.status('exited', 'The sandbox is terminating.');
      this.detach();
      this.sock.serverClose(4410, 'The sandbox was terminated.');
    }
  }

  private async open() {
    const sb = this.sb;
    if (!sb) {
      this.sock.serverClose(4404, 'Sandbox not found');
      return;
    }
    if (sb.state === 'TERMINATED' || sb.state === 'TERMINATING') {
      this.sock.serverClose(4410, 'This sandbox has been terminated.');
      return;
    }
    if (sb.state === 'SUSPENDED' || sb.state === 'SUSPENDING') {
      this.status('connecting', 'Resuming the sandbox (auto-resume on connect)');
      if (sb.state === 'SUSPENDED') setState(sb, 'PENDING');
      await wait(900);
      if ((sb.state as MState) === 'PENDING') setState(sb, 'RUNNING');
    } else if (sb.state === 'PENDING') {
      this.status('connecting', 'Waiting for the sandbox to start');
      while (sb.state === 'PENDING') await wait(300);
    } else {
      this.status('connecting', 'Opening a shell in the sandbox');
    }
    await wait(450);
    if (this.sock.readyState !== 1) return;
    this.attach(sb);
  }

  private beat: number | null = null;

  private attach(sb: MSandbox) {
    this.attached = true;
    sb.attached += 1;
    sb.lastActivity = Date.now();
    if (!this.beat) this.beat = window.setInterval(() => this.sock.serverSend(JSON.stringify({ type: 'heartbeat' })), 25_000);
    this.status('attached', 'tmux session main');
    // tmux new -A -s main: clear, redraw what the session already shows, then the status bar.
    this.out('\x1b[H\x1b[2J', false);
    if (!this.p.greeted) {
      this.p.greeted = true;
      this.out(
        `${C.bold}Lambda MicroVM sandbox${C.reset} ${C.gray}· ${sb.name} · image ${sb.image_version}${C.reset}\r\n` +
          `Claude Code is installed. Run ${C.bold}claude${C.reset} to start it.\r\n` +
          `${C.gray}API keys in this VM are placeholders; iron-proxy swaps in the real ones.${C.reset}\r\n` +
          `${C.yellow}Mock mode:${C.reset} this is a fake shell. Type ${C.bold}help${C.reset} for what it understands.\r\n\r\n`,
      );
      this.prompt();
    } else {
      this.out(this.p.transcript.slice(-6000), false);
    }
    this.drawStatusBar();
  }

  private drawStatusBar() {
    // Themed like the sandbox image should theme tmux: a dim bar from the neutral ramp, brass only
    // on the current window (status-style bg=#211d1a,fg=#969189; window-status-current-style fg=#e1b761).
    const sb = this.sb;
    const win = `0:${this.p.mode === 'claude' ? 'claude' : 'bash'}*`;
    const left = ` [main] `;
    const clock = new Date().toTimeString().slice(0, 5);
    const right = `"${sb?.name ?? 'sandbox'}" ${clock} `;
    const pad = Math.max(1, this.cols - left.length - win.length - right.length);
    const BG = '\x1b[48;2;33;29;26m';
    const FG = '\x1b[38;2;150;145;137m';
    const CUR = '\x1b[38;2;225;183;97m';
    const bar = `${BG}${FG}${left}${CUR}${win}${FG}${' '.repeat(pad)}${right}`;
    this.out(`\x1b7\x1b[1;${this.rows - 1}r\x1b[${this.rows};1H${bar}\x1b[0m\x1b8`, false);
  }

  private prompt() {
    if (this.p.mode === 'claude') this.out(`\r\n${C.gray}>${C.reset} `);
    else this.out(`${C.green}root@sandbox${C.reset}:${C.blue}~/work${C.reset}# `);
  }

  private input(s: string) {
    if (this.sb) this.sb.lastActivity = Date.now();
    for (const ch of s) {
      if (this.esc) {
        this.esc += ch;
        const intro = this.esc.length === 2 && (ch === '[' || ch === 'O');
        if (!intro && /[@-~]/.test(ch)) this.esc = '';
        continue;
      }
      if (ch === '\x1b') {
        this.esc = ch;
        continue;
      }
      if (this.busy) continue;
      if (ch === '\r' || ch === '\n') {
        const cmd = this.line;
        this.line = '';
        this.out('\r\n');
        void this.run(cmd.trim());
      } else if (ch === '\x7f' || ch === '\b') {
        if (this.line) {
          this.line = this.line.slice(0, -1);
          this.out('\b \b');
        }
      } else if (ch === '\x03') {
        this.out('^C\r\n');
        this.line = '';
        if (this.p.mode === 'claude') {
          this.p.mode = 'shell';
          this.drawStatusBar();
        }
        this.prompt();
      } else if (ch === '\x04') {
        if (!this.line && this.p.mode === 'shell') void this.run('exit');
      } else if (ch === '\x0c') {
        this.out('\x1b[H\x1b[2J');
        this.drawStatusBar();
        this.prompt();
        this.out(this.line);
      } else if (ch >= ' ') {
        this.line += ch;
        this.out(ch);
      }
    }
  }

  private async run(cmd: string) {
    if (this.p.mode === 'claude') return this.claudeTurn(cmd);
    this.busy = true;
    try {
      await this.shell(cmd);
    } finally {
      this.busy = false;
    }
    if (this.attached) this.prompt();
  }

  private async shell(cmd: string) {
    const [bin, ...args] = cmd.split(/\s+/);
    const sb = this.sb!;
    switch (bin) {
      case '':
        return;
      case 'help':
        this.out(
          `This mock shell understands:\r\n` +
            `  ls, pwd, whoami, date, env, clear, exit\r\n` +
            `  curl <url>        one request through the (fake) proxy\r\n` +
            `  git pull          GitHub traffic with the github key swapped in\r\n` +
            `  npm install       a burst of registry requests\r\n` +
            `  claude            a fake Claude Code session (each prompt costs tokens)\r\n` +
            `Mock controls:\r\n` +
            `  mock-drop-upstream   the backend loses the MicroVM and retries\r\n` +
            `  mock-drop-socket     the browser's WebSocket drops\r\n`,
        );
        return;
      case 'ls':
        this.out(`${C.blue}notes${C.reset}  ${C.blue}src${C.reset}  README.md  package.json  tsconfig.json\r\n`);
        return;
      case 'pwd':
        this.out('/root/work\r\n');
        return;
      case 'whoami':
        this.out('root\r\n');
        return;
      case 'date':
        this.out(`${new Date().toUTCString()}\r\n`);
        return;
      case 'clear':
        this.out('\x1b[H\x1b[2J');
        this.drawStatusBar();
        return;
      case 'env':
        this.out(
          `HTTPS_PROXY=http://10.50.10.10:8080\r\nHTTP_PROXY=http://10.50.10.10:8080\r\nNO_PROXY=localhost,127.0.0.1\r\n` +
            `ANTHROPIC_API_KEY=${PLACEHOLDERS.anthropic}\r\nGITHUB_TOKEN=${PLACEHOLDERS.github}\r\nDEMO_API_KEY=${PLACEHOLDERS.demo}\r\n` +
            `NODE_EXTRA_CA_CERTS=/etc/ssl/certs/iron-proxy-ca.pem\r\n`,
        );
        return;
      case 'curl': {
        const target = args.find((a) => !a.startsWith('-')) ?? '';
        let url: URL;
        try {
          url = new URL(target.includes('://') ? target : `https://${target}`);
        } catch {
          this.out('curl: (3) URL rejected: Malformed input to a URL function\r\n');
          return;
        }
        const secrets = url.hostname.endsWith('github.com') ? ['github'] : url.hostname === 'httpbin.org' ? ['demo'] : [];
        await wait(250);
        const r = proxyRequest({ method: 'GET', host: url.hostname, path: url.pathname + url.search, secrets });
        if (r.action === 'reject') this.out(`curl: (56) CONNECT tunnel failed, response 403\r\n`);
        else if (r.status === 401) this.out(`{"message":"Bad credentials","status":"401"}\r\n`);
        else this.out(`{"ok": true, "host": "${url.hostname}"}\r\n`);
        return;
      }
      case 'git':
        if (args[0] === 'pull' || args[0] === 'fetch') {
          await wait(300);
          const r = proxyRequest({ method: 'GET', host: 'github.com', path: '/my2cents/blog.git/info/refs?service=git-upload-pack', secrets: ['github'] });
          await wait(200);
          if (r.status === 401) this.out(`remote: Invalid username or token.\r\nfatal: Authentication failed for 'https://github.com/my2cents/blog.git/'\r\n`);
          else {
            proxyRequest({ method: 'POST', host: 'github.com', path: '/my2cents/blog.git/git-upload-pack', secrets: ['github'] });
            this.out('Already up to date.\r\n');
          }
          return;
        }
        this.out(`${C.gray}(mock) git ${args.join(' ')}: only pull/fetch are simulated${C.reset}\r\n`);
        return;
      case 'npm':
        if (args[0] === 'install' || args[0] === 'i') {
          for (const pkg of ['react', 'react-dom', 'vite', 'typescript', '@types%2freact', 'esbuild']) {
            proxyRequest({ method: 'GET', host: 'registry.npmjs.org', path: `/${pkg}`, secrets: [] });
            await wait(120);
          }
          this.out(`\r\nadded 212 packages, and audited 213 packages in 3s\r\n\r\nfound ${C.green}0${C.reset} vulnerabilities\r\n`);
          return;
        }
        this.out(`${C.gray}(mock) only npm install is simulated${C.reset}\r\n`);
        return;
      case 'claude':
        this.p.mode = 'claude';
        this.p.claude = startClaudeSession(sb.id);
        proxyRequest({ method: 'POST', host: 'statsig.anthropic.com', path: '/v1/initialize', secrets: [] });
        this.drawStatusBar();
        this.out(
          `\r\n${C.claude}✻${C.reset} Welcome to ${C.bold}Claude Code${C.reset} ${C.gray}(mock)${C.reset}\r\n` +
            `  ${C.gray}/exit to leave · cwd: /root/work${C.reset}\r\n`,
        );
        return;
      case 'exit':
      case 'logout':
        this.out('logout\r\n');
        this.status('exited', 'The shell exited.');
        this.detach();
        live.get(this.id)?.delete(this);
        this.p.greeted = false;
        this.p.transcript = '';
        this.sock.serverClose(1000, 'shell exited');
        return;
      case 'mock-drop-upstream': {
        this.out(`${C.gray}(mock) dropping the MicroVM connection…${C.reset}\r\n`);
        await wait(300);
        this.status('reconnecting', 'Lost the shell connection to the MicroVM. Retrying.', { retry_in_s: 4 });
        await wait(4000);
        if (this.sock.readyState !== 1) return;
        this.status('attached', 'tmux session main');
        this.out('\x1b[H\x1b[2J', false);
        this.out(this.p.transcript.slice(-6000), false);
        this.drawStatusBar();
        return;
      }
      case 'mock-drop-socket':
        this.out(`${C.gray}(mock) closing the browser socket abnormally…${C.reset}\r\n`);
        this.p.transcript += `${C.green}root@sandbox${C.reset}:${C.blue}~/work${C.reset}# `;
        await wait(300);
        this.detach();
        this.sock.serverClose(1006, '');
        return;
      default:
        this.out(`bash: ${bin}: command not found\r\n`);
    }
  }

  private async claudeTurn(text: string) {
    if (text === '/exit' || text === 'exit' || text === '/quit') {
      this.p.mode = 'shell';
      this.drawStatusBar();
      this.out('\r\n');
      this.prompt();
      return;
    }
    if (!text) {
      this.out(`${C.gray}>${C.reset} `);
      return;
    }
    this.busy = true;
    const anthropicRevoked = db.revoked.has('anthropic');
    this.out(`${C.claude}✻${C.reset} ${C.gray}Thinking…${C.reset}`);
    await wait(500);
    const r = proxyRequest({ method: 'POST', host: 'api.anthropic.com', path: '/v1/messages?beta=true', secrets: ['anthropic'] });
    await wait(700);
    this.out('\r\x1b[2K');
    if (anthropicRevoked || r.status === 401) {
      this.out(`${C.red}● API Error: 401 {"type":"error","error":{"type":"authentication_error","message":"invalid x-api-key"}}${C.reset}\r\n`);
    } else {
      if (this.p.claude) recordTurn(this.p.claude);
      const reply = REPLIES[Math.floor(Math.random() * REPLIES.length)];
      for (const line of reply) {
        this.out(`${line}\r\n`);
        await wait(160);
      }
      proxyRequest({ method: 'POST', host: 'api.anthropic.com', path: '/v1/messages?beta=true', secrets: ['anthropic'] });
    }
    this.busy = false;
    this.out(`\r\n${C.gray}>${C.reset} `);
  }
}

const REPLIES = [
  [`${C.claude}●${C.reset} I'll start by looking at how the project is laid out.`, '', `${C.claude}●${C.reset} ${C.bold}Bash${C.reset}(ls -la src)`, `  ${C.gray}⎿  12 files${C.reset}`, '', `${C.claude}●${C.reset} The entry point is ${C.bold}src/main.ts${C.reset}; tests live next to each module.`],
  [`${C.claude}●${C.reset} ${C.bold}Read${C.reset}(package.json)`, `  ${C.gray}⎿  Read 38 lines${C.reset}`, '', `${C.claude}●${C.reset} Two scripts are missing a ${C.bold}--frozen-lockfile${C.reset} flag; I can add it.`],
  [`${C.claude}●${C.reset} ${C.bold}Bash${C.reset}(npm test)`, `  ${C.gray}⎿  42 passed, 1 failed (auth.spec.ts:88)${C.reset}`, '', `${C.claude}●${C.reset} The failure is a clock assumption in the token expiry test. Fixing it now.`],
];

function wait(ms: number) {
  return new Promise<void>((r) => window.setTimeout(r, ms));
}

export function allowlist() {
  return ALLOWLIST;
}
