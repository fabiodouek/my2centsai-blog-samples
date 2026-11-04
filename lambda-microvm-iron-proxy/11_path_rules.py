"""Test 10: path rules and `require`, transparent vs explicit. strict=true limits GitHub to one repository with
path rules and marks the GitHub secret `require: true`. This applies strict=true, runs the same probes in a
transparent MicroVM (no proxy settings) and in an explicit-mode one (HTTPS_PROXY to the NLB on 8080), then
reverts to strict=false.

In transparent mode iron-proxy sees the real request, so the rules match real paths and headers. In explicit
mode every request is a CONNECT tunnel first, and iron-proxy 0.55.0 checks the tunnel against path rules and
`require` with an empty path and no headers (PR #295, issue #222), so GitHub is refused entirely.

git sends its first request without credentials and only adds them after a 401; `require` answers that first
request with a 403, so git gets the placeholder up front with http.extraHeader.

  python 11_path_rules.py          # apply strict=true, test both modes, revert to strict=false
  python 11_path_rules.py --keep   # leave strict=true applied afterwards"""

import sys
import time

from common import (REPO, apply_infra, audit_line, banner, check, is_request, is_tunnel, proxy_audit, run, start_log,
                    summary)

KEEP = "--keep" in sys.argv
start_log("11_path_rules")
BASIC = "Authorization: Basic $(printf 'x-access-token:%s' \"$GH_TOKEN\" | base64 -w0)"
PROBES = [
    ("gh api user", "gh api user --jq .login 2>&1"),
    (f"gh api repos/{REPO}", f"gh api repos/{REPO} --jq .full_name 2>&1"),
    (f"git ls-remote {REPO}", f"git ls-remote https://github.com/{REPO}.git HEAD 2>&1 | cut -c1-110"),
    ("  the same, placeholder sent up front (http.extraHeader)",
     f"git -c http.extraHeader=\"{BASIC}\" ls-remote https://github.com/{REPO}.git HEAD 2>&1 | cut -c1-110"),
    (f"git clone {REPO} (http.extraHeader)",
     f"cd /root && rm -rf r2 && git -c http.extraHeader=\"{BASIC}\" clone -q https://github.com/{REPO}.git r2 2>&1 | tail -1; "
     "git -C r2 log --oneline -1 2>&1 | cut -c1-60"),
    ("gh api repos/torvalds/linux (other repo)", "gh api repos/torvalds/linux --jq .full_name 2>&1"),
    ("gh gist create (POST /gists)", "echo hi > /tmp/g.txt; gh gist create /tmp/g.txt 2>&1"),
    ("git ls-remote torvalds/linux (http.extraHeader)",
     f"git -c http.extraHeader=\"{BASIC}\" ls-remote https://github.com/torvalds/linux.git HEAD 2>&1 | cut -c1-110"),
    ("curl api.github.com/user without the placeholder", "curl -s -o /dev/null -w '%{http_code}' https://api.github.com/user"),
    ("curl api.github.com/user with the placeholder",
     "curl -s -o /dev/null -w '%{http_code}' https://api.github.com/user -H \"Authorization: token $GH_TOKEN\""),
    ("python urllib, proxies disabled",
     "python3 -c \"import urllib.request as u; print(u.build_opener(u.ProxyHandler({})).open('https://httpbin.org/get', timeout=10).status)\" 2>&1 | tail -1"),
    ("node fetch (never reads HTTPS_PROXY)",
     "node -e \"fetch('https://httpbin.org/get').then(r=>console.log(r.status)).catch(e=>console.log('FAIL', e.cause?.code||e.message))\""),
    ("Claude Code (api.anthropic.com has no path rule)", "cd /root && claude -p 'Reply with exactly: ok' --max-turns 1 < /dev/null 2>&1 | tail -1"),
]


def run_probes(mode: str) -> tuple[dict, list]:
    since = int(time.time() * 1000)
    vm = run(f"11-{mode}", mode=mode)
    banner(f"{mode.capitalize()}: {'no proxy settings' if mode == 'transparent' else 'HTTPS_PROXY to the NLB on 8080'}")
    print("api.github.com resolves to:", vm.sh("getent ahosts api.github.com | awk '/STREAM/ {print $1}' | tr '\\n' ' '"),
          "| proxy env:", vm.sh("echo ${HTTPS_PROXY:-none}"))
    res = {}
    for label, cmd in PROBES:
        out = vm.sh(cmd, timeout=120)
        res[label] = out.splitlines()[-1][:140] if out else ""
        print(f"{label:56} {res[label]}")
    vm.terminate()
    until = int(time.time() * 1000)
    recs = [a for a in proxy_audit(since, "github", until_ms=until) if is_request(a)]
    banner(f"iron-proxy audit, {mode} (github)")
    for a in recs:
        print(audit_line(a))
    return res, recs


banner("terraform apply strict=true")
try:
    print(f"applied; the new iron-proxy task is serving after {apply_infra(strict=True):.0f}s")
    tr, tr_recs = run_probes("transparent")
    ex, ex_recs = run_probes("explicit")
finally:
    if not KEEP:
        banner("terraform apply strict=false (revert)")
        print(f"reverted; the new iron-proxy task is serving after {apply_infra(strict=False):.0f}s")

banner("Side by side")
print(f"{'':56} {'transparent':26} explicit")
for label, _ in PROBES:
    print(f"{label:56} {tr[label][:26]:26} {ex[label][:40]}")

L = [label for label, _ in PROBES]
# Transparent: one repository works, everything else on GitHub is refused by the proxy
check("transparent: gh api user", tr[L[0]] and "403" not in tr[L[0]] and "orbidden" not in tr[L[0]], tr[L[0]])
check("transparent: the allowed repo", tr[L[1]] == REPO, tr[L[1]])
check("transparent: git without credentials up front is refused by require", "403" in tr[L[2]] or "fatal" in tr[L[2]], tr[L[2]])
check("transparent: git with http.extraHeader passes require", "HEAD" in tr[L[3]], tr[L[3]])
check("transparent: git clone with http.extraHeader", tr[L[4]] and "fatal" not in tr[L[4]], tr[L[4]])
check("transparent: another repo refused over REST", tr[L[5]] != "torvalds/linux", tr[L[5]])
check("transparent: gist refused", "403" in tr[L[6]], tr[L[6]])
check("transparent: another repo refused over git", "HEAD" not in tr[L[7]], tr[L[7]])
check("transparent: no placeholder, refused (require)", tr[L[8]] == "403", tr[L[8]])
check("transparent: with the placeholder, 200", tr[L[9]] == "200", tr[L[9]])
check("transparent: python urllib works", tr[L[10]] == "200", tr[L[10]])
check("transparent: node fetch works", tr[L[11]] == "200", tr[L[11]])
check("transparent: Claude Code works", tr[L[12]].strip().lower().startswith("ok"), tr[L[12]])
check("transparent: no CONNECT records", tr_recs and not any(is_tunnel(a) for a in tr_recs))
check("transparent: a require rejection logged by secrets", any(a.get("rejected_by") == "secrets" for a in tr_recs))
# Explicit: the CONNECT carries no path and no Authorization header, so GitHub is refused, even the allowed repo
check("explicit: gh api user refused at CONNECT", "orbidden" in ex[L[0]] or "403" in ex[L[0]], ex[L[0]])
check("explicit: even the allowed repo refused", ex[L[1]] != REPO, ex[L[1]])
check("explicit: git with http.extraHeader refused at CONNECT", "HEAD" not in ex[L[3]], ex[L[3]])
check("explicit: Claude Code still works", ex[L[12]].strip().lower().startswith("ok"), ex[L[12]])
check("explicit: GitHub CONNECTs rejected in the audit log",
      any(a["audit"].get("method") == "CONNECT" and a["audit"].get("action") == "reject" for a in ex_recs), f"{len(ex_recs)} records")
summary()
