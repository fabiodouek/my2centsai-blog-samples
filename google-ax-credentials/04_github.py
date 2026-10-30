"""Step 4: gh and git against a private repo, with no token in the sandbox.

gh sends `Authorization: token <GH_TOKEN>` to api.github.com; git sends the
header we configure with http.extraHeader. The gateway replaces both values."""

from common import PLACEHOLDER, banner, create_task, inject, load_env, redact, set_policy, sh, show

NAME = "cred-gh"
env = load_env()
REPO, REAL = env["GITHUB_REPO"], env["GITHUB_TOKEN"]
GITHUB_RULES = [
    inject("api.github.com", "Authorization", "github", "token", prefix="Bearer "),
    inject("github.com", "Authorization", "github", "basic", prefix="Basic "),
]
GIT_SETUP = ("git config --global http.https://github.com/.extraHeader 'Authorization: Basic placeholder' && "
             "git config --global user.name 'ax-agent' && git config --global user.email 'ax-agent@example.com'")

banner("the sandbox's view: placeholders only")
create_task(NAME, {"GH_TOKEN": PLACEHOLDER})
print(sh(NAME, GIT_SETUP))
show(NAME, "echo GH_TOKEN=$GH_TOKEN; git config --global --get-regexp extraheader")

banner("without a policy: no DNS for github.com")
show(NAME, f"gh api repos/{REPO} --jq .full_name 2>&1 | tail -1")

set_policy(NAME, GITHUB_RULES)

banner("gh against the REST API")
print(redact(sh(NAME, f"gh api repos/{REPO} --jq '\"\\(.full_name) private=\\(.private)\"'"), REAL))
print(redact(sh(NAME, "gh api user --jq .login 2>&1 | tail -1"), REAL))

banner("git clone, commit, push a branch")
print(redact(sh(NAME, f"cd /workspace && rm -rf repo && git clone -q https://github.com/{REPO}.git repo && "
                      "cd repo && git log --oneline -1 && git checkout -q -b ax-demo-push && "
                      "date > ax-demo.txt && git add ax-demo.txt && git commit -qm 'ax: push test' && "
                      "git push -q origin ax-demo-push 2>&1 | tail -2 && echo pushed"), REAL))

banner("delete the branch again")
print(redact(sh(NAME, "cd /workspace/repo && git push -q origin --delete ax-demo-push && echo deleted"), REAL))
