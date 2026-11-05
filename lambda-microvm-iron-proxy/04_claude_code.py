"""Test 3: Claude Code with a placeholder Anthropic key and no proxy settings. The MicroVM has only the
placeholder; api.anthropic.com resolves to the NLB, and iron-proxy swaps in the real key. Also: a tool-using
run, a streaming run, and a run with no key at all."""

import time

from common import banner, check, claude, is_tunnel, proxy_audit, run, start_log, summary

start_log("04_claude_code")
since = int(time.time() * 1000)
vm = run("04-claude")

banner("What the MicroVM has")
print(vm.sh("claude --version; echo ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY | cut -c1-40; echo HTTPS_PROXY=${HTTPS_PROXY:-unset}; "
            "getent hosts api.anthropic.com | head -1"))

banner("claude -p, transparently through the proxy")
t0 = time.perf_counter()
r = claude(vm, "Reply with exactly: the proxy holds the key", flags="--max-turns 1")
print(f"is_error={r['is_error']} result={r['result']!r} turns={r.get('num_turns')} "
      f"cost=${r.get('total_cost_usd', 0):.4f} wall={time.perf_counter() - t0:.1f}s model={list((r.get('modelUsage') or {}).keys())}")
check("claude -p answers with the placeholder key", not r["is_error"] and "proxy holds the key" in r["result"].lower(), repr(r["result"]))

banner("A tool-using run (writes and reads a file)")
r = claude(vm, "Create /root/hello.py that prints the sum of 1..100, run it with python3, and reply with only the number.",
           flags="--dangerously-skip-permissions --max-turns 6")
print(f"is_error={r['is_error']} result={r['result']!r} turns={r.get('num_turns')} cost=${r.get('total_cost_usd', 0):.4f}")
check("tool-using run returns 5050", "5050" in str(r["result"]), repr(r["result"]))

banner("Streaming output (stream-json lines arrive through the MITM)")
out = vm.sh("cd /root && claude -p 'Count from 1 to 40, one number per line.' --output-format stream-json --verbose "
            "--include-partial-messages --max-turns 1 < /dev/null | grep -c content_block_delta", timeout=180)
print("token deltas streamed through the proxy:", out)
check("streaming deltas arrive", out.strip().isdigit() and int(out.strip()) > 1, out)

banner("Without the placeholder")
out = vm.sh("cd /root && env -u ANTHROPIC_API_KEY claude -p 'hi' --max-turns 1 < /dev/null 2>&1 | head -3", timeout=60)
print(out)

banner("iron-proxy audit (api.anthropic.com), summarized")
counts, recs = {}, [rec for rec in proxy_audit(since, "anthropic") if (rec.get("audit") or {}).get("host")]
for rec in recs:
    a = rec["audit"]
    swapped = any((t.get("annotations") or {}).get("swapped") for t in rec.get("request_transforms") or [])
    path = a.get("path", "").split("?")[0]
    key = (a.get("method"), a.get("host", "") + (path if not path.startswith("/api/eval/") else "/api/eval/<id>"),
           "key swapped" if swapped else "no key")
    counts[key] = counts.get(key, 0) + 1
for (method, path, sw), n in sorted(counts.items(), key=lambda kv: -kv[1]):
    print(f"{n:4d}  {method:5} {path:62} {sw}")
check("the real key was swapped in on api.anthropic.com", any(k[1].startswith("api.anthropic.com") and k[2] == "key swapped" for k in counts))
check("no CONNECT: every request intercepted directly", recs and not any(is_tunnel(r) for r in recs))
vm.terminate()
summary()
