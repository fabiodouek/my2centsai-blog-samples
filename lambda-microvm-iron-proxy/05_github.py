"""Test 4: git and gh with no GitHub token and no proxy settings in the MicroVM. gh sends the placeholder as
`Authorization: token ...` to api.github.com; git's credential helper sends it as HTTP Basic to github.com.
iron-proxy swaps the placeholder in both (it decodes Basic first)."""

import time

from common import REPO, audit_line, banner, check, is_request, proxy_audit, run, start_log, summary

start_log("05_github")
since = int(time.time() * 1000)
branch = f"iron-proxy-demo-git-{int(time.time())}"
vm = run("05-github")

banner("What the MicroVM has")
print(vm.sh("echo GH_TOKEN=$GH_TOKEN; echo HTTPS_PROXY=${HTTPS_PROXY:-unset}; git config --system --get credential.helper; "
            "gh --version | head -1"))

banner("gh api user (token header on api.github.com)")
out = vm.sh("gh api user --jq .login")
print(out)
check("gh api user returns the token's login", out and " " not in out.strip() and "error" not in out.lower(), out)

banner(f"git clone {REPO} (Basic auth on github.com)")
out = vm.sh(f"cd /root && git clone -q https://github.com/{REPO}.git repo && git -C repo log --oneline -1")
print(out)
check("git clone works", out and "fatal" not in out, out)

banner(f"git push to {branch}")
out = vm.sh(f"cd /root/repo && git checkout -q -b {branch} && date > iron-proxy-git.txt && git add . && "
            f"git commit -qm 'iron-proxy demo: push through the proxy' && git push -q origin {branch} 2>&1 | tail -2; "
            f"git ls-remote origin {branch} | cut -c1-12")
print(out)
check("git push works (the branch exists upstream)", out and len(out.splitlines()[-1].strip()) == 12, out)

banner("What the token can't do (GitHub's scoping, not the proxy's)")
out = vm.sh("gh api repos/torvalds/linux/collaborators --jq length 2>&1 | tail -1")
print("other repo:", out)
check("another repo refused by GitHub", "403" in out or "not accessible" in out, out)
out = vm.sh("echo hi > /tmp/g.txt && gh gist create /tmp/g.txt 2>&1 | tail -1")
print("create a gist:", out)
check("gist refused by GitHub", "403" in out or "not accessible" in out, out)

banner("Clean up the branch")
print(vm.sh(f"cd /root/repo && git push -q origin --delete {branch} 2>&1 | tail -1; echo deleted"))

banner("iron-proxy audit (github)")
recs = [a for a in proxy_audit(since, "github") if is_request(a)]
for a in recs:
    print(audit_line(a))
check("token swapped on api.github.com and github.com",
      all(any(a["audit"].get("host") == h and "swapped" in str(a.get("request_transforms")) for a in recs) for h in ("api.github.com", "github.com")))
vm.terminate()
summary()
