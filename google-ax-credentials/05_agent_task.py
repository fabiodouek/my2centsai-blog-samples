"""Step 5: end to end. Claude Code, holding only placeholders, clones the repo,
makes a change, pushes a branch and opens a pull request."""

import time

from common import PLACEHOLDER, banner, create_task, inject, load_env, redact, set_policy, sh

NAME = "cred-agent"
env = load_env()
REPO, SECRETS = env["GITHUB_REPO"], (env["GITHUB_TOKEN"], env["ANTHROPIC_API_KEY"])
BRANCH = f"ax-agent-{int(time.time())}"

banner("one task, three injection rules, no real credential inside")
create_task(NAME, {
    "ANTHROPIC_API_KEY": PLACEHOLDER,
    "GH_TOKEN": PLACEHOLDER,
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "IS_SANDBOX": "1",  # lets Claude Code skip permission prompts as root, inside a sandbox
})
set_policy(NAME, [
    inject("api.anthropic.com", "x-api-key", "anthropic", "api-key"),
    inject("api.github.com", "Authorization", "github", "token", prefix="Bearer "),
    inject("github.com", "Authorization", "github", "basic", prefix="Basic "),
])
print(sh(NAME, "git config --global http.https://github.com/.extraHeader 'Authorization: Basic placeholder' && "
               "git config --global user.name 'ax-agent' && git config --global user.email 'ax-agent@example.com' && "
               f"cd /workspace && git clone -q https://github.com/{REPO}.git repo && echo cloned"))

PROMPT = (f"In this git repo, create a new branch named {BRANCH}. Add a file NOTES.md with a short "
          "paragraph explaining that this pull request was opened by Claude Code running in a Google AX "
          "sandbox on Amazon EKS, and that the sandbox never held the Anthropic key or the GitHub token. "
          "Commit it, push the branch, and open a pull request with gh. Reply with only the PR URL.")

banner("Claude Code does the rest")
t0 = time.time()
out = sh(NAME, f"cd /workspace/repo && claude -p {PROMPT!r} --model claude-sonnet-5-5 "
               "--dangerously-skip-permissions --max-turns 20 < /dev/null")
print(redact(out, *SECRETS))
print(f"  ({time.time() - t0:.0f} s)")

banner("the pull request, seen from GitHub")
print(redact(sh(NAME, f"gh pr list --repo {REPO} --head {BRANCH} --json number,title,url,author "
                      "--jq '.[] | \"#\\(.number) \\(.title)\\n\\(.url)\\nauthor: \\(.author.login)\"'"), *SECRETS))
