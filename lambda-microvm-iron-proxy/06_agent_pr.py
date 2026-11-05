"""Test 5: Claude Code does a real task end to end through iron-proxy: clone, change, push, open a pull request.
None of the three credentials is in the MicroVM, and neither is a proxy setting. The script then closes the
pull request and deletes the branch, so the test repo doesn't fill up."""

import re
import time

from common import REPO, banner, check, claude, is_request, proxy_audit, run, start_log, summary

start_log("06_agent_pr")
since = int(time.time() * 1000)
branch = f"iron-proxy-demo-agent-{int(time.time())}"
PROMPT = f"""Clone https://github.com/{REPO}.git into ./repo with git.
Create a branch named {branch}. Add a file docs/lambda-microvm-iron-proxy.md with two sentences explaining
what an egress proxy that injects credentials does for a sandbox on AWS Lambda MicroVMs. Commit it, push the
branch, then open a pull request with `gh pr create --repo {REPO} --fill`. Print only the pull request URL
as your final line."""

vm = run("06-agent")
banner("Claude Code: clone, commit, push, open PR")
t0 = time.perf_counter()
r = claude(vm, PROMPT, flags="--dangerously-skip-permissions --max-turns 30", timeout=900)
print(f"is_error={r['is_error']} turns={r.get('num_turns')} cost=${r.get('total_cost_usd', 0):.4f} "
      f"wall={time.perf_counter() - t0:.0f}s")
print("result:", r["result"])
urls = re.findall(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+", r["result"] or "")
check("Claude Code opened a pull request", bool(urls), (r["result"] or "")[-120:])

banner("Requests the proxy saw, by host")
counts = {}
for a in proxy_audit(since):
    if is_request(a):
        k = (a["audit"]["host"], a["audit"].get("action"))
        counts[k] = counts.get(k, 0) + 1
for (host, action), n in sorted(counts.items()):
    print(f"{n:4d}  {action:6} {host}")
check("the agent's traffic went through iron-proxy (Anthropic and GitHub)",
      any(h == "api.anthropic.com" for h, _ in counts) and any(h == "github.com" for h, _ in counts))

if urls:
    banner("Close the pull request and delete its branch")
    out = vm.sh(f"cd /root/repo 2>/dev/null; gh pr close {urls[-1]} --delete-branch "
                f"--comment 'Closed by 06_agent_pr.py (demo run).' 2>&1 | tail -2; "
                f"gh pr view {urls[-1]} --json state --jq .state", timeout=90)
    print(out)
    check("pull request closed", out.strip().endswith("CLOSED"), out.splitlines()[-1] if out else "")
vm.terminate()
summary()
