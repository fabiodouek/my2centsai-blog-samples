import { Fragment, useMemo, useState } from 'react';
import { ChevronDown, ChevronRight, TriangleAlert } from 'lucide-react';
import { hub, useFeedPanel, useHub } from '../../lib/events';
import { fmtTime } from '../../lib/format';
import { usePref } from '../../lib/hooks';
import { useStore } from '../../lib/store';
import type { DnsEvent } from '../../lib/types';
import { Empty, Note, Segmented } from '../ui/primitives';
import { FeedPendingEmpty, FeedStatus } from './FeedStatus';

type Filter = 'all' | 'nx' | 'off';
type Scope = 'sandboxes' | 'everything';
const RENDER_CAP = 300;

function matches(name: string, pattern: string) {
  const p = pattern.toLowerCase().replace(/\.$/, '');
  if (p.startsWith('*.')) {
    const base = p.slice(2);
    return name === base || name.endsWith(`.${base}`);
  }
  return name === p;
}

function ipv4(ip: string): number | null {
  const parts = ip.split('.');
  if (parts.length !== 4) return null;
  let n = 0;
  for (const p of parts) {
    const v = Number(p);
    if (!/^\d{1,3}$/.test(p) || v > 255) return null;
    n = n * 256 + v;
  }
  return n;
}

/** IPv4 only (the VPC is IPv4); arithmetic instead of bit ops so /0../32 stay unsigned. */
function inCidr(ip: string, cidr: string): boolean {
  const [base, bits = '32'] = cidr.split('/');
  const a = ipv4(ip);
  const b = ipv4(base);
  const n = Number(bits);
  if (a === null || b === null || !Number.isInteger(n) || n < 0 || n > 32) return false;
  const size = 2 ** (32 - n);
  return Math.floor(a / size) === Math.floor(b / size);
}

/**
 * Whether a name is on the proxy allowlist: exact when the backend sends proxy_allowlist,
 * otherwise inferred from the proxy feed (allowed vs rejected_by=allowlist). null = unknown.
 */
function useAllowCheck(): (name: string) => boolean | null {
  const allowlist = useStore((s) => s.config?.allowlist ?? null);
  const proxy = hub.proxy;
  return useMemo(() => {
    if (allowlist && allowlist.length) {
      return (name: string) => allowlist.some((p) => matches(name.toLowerCase(), p));
    }
    const allowed = new Set<string>();
    const denied = new Set<string>();
    for (const e of proxy) {
      if (e.action === 'allow') allowed.add(e.host.toLowerCase());
      else if (e.rejectedBy === 'allowlist') denied.add(e.host.toLowerCase());
    }
    return (name: string) => (allowed.has(name) ? true : denied.has(name) ? false : null);
  }, [allowlist, proxy]);
}

export function DnsPanel() {
  useHub();
  useFeedPanel();
  const [filter, setFilter] = useState<Filter>('all');
  const [scopePref, setScope] = usePref<Scope>('ws.dnsScope.v1', 'sandboxes');
  const [open, setOpen] = useState<number | null>(null);
  const allowed = useAllowCheck();
  const exact = useStore((s) => Boolean(s.config?.allowlist?.length));
  const cidrs = useStore((s) => s.config?.sandboxCidrs ?? []);
  // The query log is part 2's sandbox VPC, where only the connector's ENIs live: every query is a sandbox's, so
  // there's nothing to scope by. (Kept for a backend that logs a whole VPC: it sends the sandbox subnets.)
  const scoped = false && cidrs.length > 0;
  const scope: Scope = scoped ? scopePref : 'everything';
  const everything = hub.dns;

  const fromSandbox = useMemo(() => {
    const cache = new Map<string, boolean>();
    return (src: string) => {
      let v = cache.get(src);
      if (v === undefined) {
        v = cidrs.some((c) => inCidr(src, c));
        cache.set(src, v);
      }
      return v;
    };
  }, [cidrs]);
  const sandboxRows = useMemo(() => everything.filter((e) => fromSandbox(e.srcaddr)), [everything, fromSandbox]);
  const all = scope === 'sandboxes' ? sandboxRows : everything;

  const isNx = (e: DnsEvent) => e.rcode.toUpperCase() === 'NXDOMAIN';
  const isOff = (e: DnsEvent) => allowed(e.query.toLowerCase()) === false;

  const counts = useMemo(
    () => ({ all: all.length, nx: all.filter(isNx).length, off: all.filter(isOff).length }),
    [all, allowed],
  );
  const rows = useMemo(() => {
    const f = filter === 'nx' ? all.filter(isNx) : filter === 'off' ? all.filter(isOff) : all;
    return f.slice(0, RENDER_CAP);
  }, [all, filter, allowed]);

  return (
    <div className="pane">
      <div className="pane-head">
        <Note>
          {scoped ? (
            <>
              Route 53 Resolver query log. <strong>Sandboxes</strong>: lookups from the sandbox subnets (
              {cidrs.join(', ')}), not tied to one sandbox. <strong>Everything</strong> adds the workspace backend’s
              and iron-proxy’s own. A warning icon marks names off the proxy allowlist.
            </>
          ) : (
            <>
              Every sandbox on this proxy, not just this one. Route 53 Resolver query log for the sandbox VPC: all
              sandboxes share the connector’s addresses, so a query can’t be tied to one sandbox. DNS Firewall answers
              every name with a CNAME to iron-proxy’s load balancer. A warning icon marks names off the proxy allowlist.
            </>
          )}
        </Note>
        {scoped ? (
          <div className="filter-row">
            <Segmented
              label="Whose lookups"
              value={scope}
              onChange={(v) => {
                setScope(v);
                setOpen(null);
              }}
              options={[
                { value: 'sandboxes', label: 'Sandboxes', count: sandboxRows.length },
                { value: 'everything', label: 'Everything', count: everything.length },
              ]}
            />
            <span className="filter-spacer" />
            <FeedStatus />
          </div>
        ) : null}
        <div className="filter-row">
          <Segmented
            label="Filter queries"
            value={filter}
            onChange={(v) => {
              setFilter(v);
              setOpen(null);
            }}
            options={[
              { value: 'all', label: 'All', count: counts.all },
              { value: 'nx', label: 'NXDOMAIN', count: counts.nx },
              { value: 'off', label: 'Not on allowlist', count: counts.off },
            ]}
          />
          {scoped ? null : (
            <>
              <span className="filter-spacer" />
              <FeedStatus />
            </>
          )}
        </div>
      </div>
      <div className="feed">
        {rows.length === 0 && everything.length === 0 && hub.feedPending ? (
          <FeedPendingEmpty what="DNS queries" />
        ) : rows.length === 0 ? (
          <Empty title={everything.length === 0 ? 'Waiting for queries' : 'Nothing matches this filter'}>
            {everything.length === 0
              ? 'DNS queries appear here as the resolver logs them, usually within a few seconds.'
              : all.length === 0
                ? 'No lookups from the sandbox subnets in what’s loaded. Everything shows the rest of the VPC.'
                : filter === 'nx'
                  ? 'No query came back NXDOMAIN in what’s loaded.'
                  : 'Every queried name is on the proxy allowlist.'}
          </Empty>
        ) : (
          <table className="table dns-table">
            <thead>
              <tr>
                <th scope="col">Time</th>
                <th scope="col">Name</th>
                <th scope="col">Type</th>
                <th scope="col">Result</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((e) => {
                const nx = isNx(e);
                const onList = allowed(e.query.toLowerCase());
                const isOpen = open === e.key;
                const toggle = () => setOpen(isOpen ? null : e.key);
                return (
                  <Fragment key={e.key}>
                    <tr
                      className={`dns-row${nx ? ' is-nx' : ''}${isOpen ? ' is-open' : ''}`}
                      tabIndex={0}
                      aria-expanded={isOpen}
                      onClick={toggle}
                      onKeyDown={(k) => {
                        if (k.key === 'Enter' || k.key === ' ') {
                          k.preventDefault();
                          toggle();
                        }
                      }}
                    >
                      <td className="tabular dns-time">{fmtTime(e.ts)}</td>
                      <td className={`dns-name${onList === false ? ' is-off' : ''}`}>
                        {isOpen ? (
                          <ChevronDown size={12} className="dns-chevron" aria-hidden />
                        ) : (
                          <ChevronRight size={12} className="dns-chevron" aria-hidden />
                        )}
                        {onList === false ? (
                          <TriangleAlert size={12} className="dns-flag" aria-label="Not on the proxy allowlist" />
                        ) : null}
                        <span className="mono" title={e.query}>
                          {e.query}
                        </span>
                      </td>
                      <td className="dns-type">{e.qtype}</td>
                      <td className={`dns-rcode${nx ? ' tone-deny' : ''}`}>{e.rcode}</td>
                    </tr>
                    {isOpen ? (
                      <tr className="dns-detail-row">
                        <td colSpan={4}>
                          <dl className="feed-detail dns-detail">
                            <dt>Name</dt>
                            <dd className="mono wrap">{e.query}</dd>
                            <dt>From</dt>
                            <dd>
                              <span className="mono">{e.srcaddr || 'unknown'}</span>
                              {scoped && e.srcaddr
                                ? fromSandbox(e.srcaddr)
                                  ? ' · a sandbox subnet'
                                  : ' · not a sandbox subnet (workspace backend or iron-proxy)'
                                : ''}
                            </dd>
                            <dt>Result</dt>
                            <dd>
                              {e.qtype} · {e.rcode}
                            </dd>
                            <dt>Allowlist</dt>
                            <dd>
                              {onList === false
                                ? exact
                                  ? 'Not on the proxy allowlist; iron-proxy would reject requests to it'
                                  : 'iron-proxy rejected a request to this host (allowlist)'
                                : onList
                                  ? 'On the proxy allowlist'
                                  : 'Unknown'}
                            </dd>
                            <dt>Time</dt>
                            <dd className="tabular">{new Date(e.ts).toISOString().replace('T', ' ').replace('Z', ' UTC')}</dd>
                          </dl>
                        </td>
                      </tr>
                    ) : null}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
