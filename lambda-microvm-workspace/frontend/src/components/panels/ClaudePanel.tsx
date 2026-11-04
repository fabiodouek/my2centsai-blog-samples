import { Eye, Plug } from 'lucide-react';
import type { SandboxLive } from '../../lib/live';
import { fmtAgo, fmtTokens, fmtTokensShort, fmtUsd, shortId } from '../../lib/format';
import { useNow } from '../../lib/hooks';
import type { ClaudeUsage, Sandbox } from '../../lib/types';
import { Empty, SkeletonRows } from '../ui/primitives';
import { AwakeHint } from './AwakeHint';

export function ClaudePanel({ sandbox, live }: { sandbox: Sandbox | null; live: SandboxLive }) {
  const now = useNow();
  if (!sandbox) return <Empty title="No sandbox selected">Pick a sandbox to see its Claude Code token usage.</Empty>;

  const liveOn = live.attached && sandbox.state === 'RUNNING';
  const data = liveOn ? (live.usage.data ?? live.lastUsage?.data ?? null) : (live.lastUsage?.data ?? null);
  const stale = !liveOn && live.lastUsage !== null;

  return (
    <div className="pane">
      <div className="pane-head">
        <div className="pane-title-row">
          <h2 className="pane-title">Token usage in {sandbox.name}</h2>
          {liveOn ? <AwakeHint what="Usage refreshes every 10 s" label="Reading usage keeps this sandbox awake" /> : null}
        </div>
      </div>
      <div className="pane-body">
        {!liveOn && !data ? (
          <Empty
            icon={<Plug size={18} aria-hidden />}
            title="Attach a terminal to read usage"
            >
            Usage is read from Claude Code’s logs inside the sandbox, and any request to the sandbox keeps it awake.
            So it only refreshes, every 10 s, while a terminal is attached.
          </Empty>
        ) : null}
        {liveOn && !data && live.usage.loading ? <SkeletonRows rows={4} /> : null}
        {liveOn && !data && live.usage.error ? <Empty title="Couldn’t read usage">{live.usage.error.message}</Empty> : null}
        {data ? <UsageView data={data} /> : null}
        {stale ? (
          <p className="pane-foot">
            <Eye size={12} aria-hidden /> As of {fmtAgo(live.lastUsage!.at, now)}. Attach to refresh.
          </p>
        ) : live.usage.updatedAt && liveOn ? (
          <p className="pane-foot">
            Updated {fmtAgo(live.usage.updatedAt, now)}
            {live.usage.error ? ` · last refresh failed: ${live.usage.error.message}` : ''}
          </p>
        ) : null}
      </div>
    </div>
  );
}

function UsageView({ data }: { data: ClaudeUsage }) {
  const t = data.totals;
  const total = t.inputTokens + t.outputTokens + t.cacheReadTokens + t.cacheCreationTokens;
  if (data.sessions.length === 0) {
    return (
      <Empty title="No Claude Code sessions yet">
        Start Claude Code from the toolbar. Sessions show up here after the first reply.
      </Empty>
    );
  }
  return (
    <>
      <dl className="usage-summary">
        <div>
          <dt>Estimated cost</dt>
          <dd className="usage-num tabular">{fmtUsd(t.estCostUsd)}</dd>
        </div>
        <div>
          <dt>Tokens</dt>
          <dd className="usage-num tabular">{fmtTokens(total)}</dd>
        </div>
        <div>
          <dt>Turns</dt>
          <dd className="usage-num tabular">{t.turns.toLocaleString()}</dd>
        </div>
        <div>
          <dt>Sessions</dt>
          <dd className="usage-num tabular">{data.sessions.length}</dd>
        </div>
      </dl>

      <div className="table-scroll">
        <table className="table">
          <thead>
            <tr>
              <th scope="col">Session</th>
              <th scope="col" className="num" title="Input tokens">Input</th>
              <th scope="col" className="num" title="Output tokens">Output</th>
              <th scope="col" className="num" title="Cache read tokens">
                Cache
                <br />
                read
              </th>
              <th scope="col" className="num" title="Cache creation tokens">
                Cache
                <br />
                write
              </th>
              <th scope="col" className="num">Cost</th>
            </tr>
          </thead>
          <tbody>
            {data.sessions.map((s) => (
              <tr key={s.sessionId}>
                <th scope="row">
                  <span className="cell-main mono" title={s.sessionId}>
                    {shortId(s.sessionId)}
                  </span>
                  <span className="cell-sub" title={`${s.model} · ${s.project}`}>
                    {s.turns} {s.turns === 1 ? 'turn' : 'turns'} · {s.model || 'unknown model'}
                  </span>
                </th>
                <td className="num tabular">{fmtTokensShort(s.inputTokens)}</td>
                <td className="num tabular">{fmtTokensShort(s.outputTokens)}</td>
                <td className="num tabular">{fmtTokensShort(s.cacheReadTokens)}</td>
                <td className="num tabular">{fmtTokensShort(s.cacheCreationTokens)}</td>
                <td className="num tabular" title={s.pricedAs ? `Priced as ${s.pricedAs}` : 'Model not in the pricing table'}>
                  {fmtUsd(s.estCostUsd)}
                </td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <th scope="row">Total</th>
              <td className="num tabular">{fmtTokensShort(t.inputTokens)}</td>
              <td className="num tabular">{fmtTokensShort(t.outputTokens)}</td>
              <td className="num tabular">{fmtTokensShort(t.cacheReadTokens)}</td>
              <td className="num tabular">{fmtTokensShort(t.cacheCreationTokens)}</td>
              <td className="num tabular">{fmtUsd(t.estCostUsd)}</td>
            </tr>
          </tfoot>
        </table>
      </div>
      <p className="pane-note">
        {pricingNote(data)}
        {data.unpricedModels.length ? ` Not priced: ${data.unpricedModels.join(', ')}.` : ''}
      </p>
    </>
  );
}

/**
 * "Estimated from Anthropic list prices as of 2026-11-03.": the backend's note when it sends one, else the same
 * sentence built from source and as_of (never a source that leaks build-time tooling notes).
 */
function pricingNote(data: ClaudeUsage): string {
  const end = (t: string) => (/[.!?]$/.test(t) ? t : `${t}.`);
  const note = data.pricingNote?.trim();
  if (note && !/skill/i.test(note)) return end(note);
  const src = data.pricingSource?.trim();
  const source = src && !/skill/i.test(src) ? src : 'Anthropic';
  return end(`Estimated from ${source} list prices${data.pricingAsOf ? ` as of ${data.pricingAsOf}` : ''}`);
}
