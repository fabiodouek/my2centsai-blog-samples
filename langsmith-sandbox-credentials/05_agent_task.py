"""Test 4: Claude Code does a real task end to end: clone, change, push, open a PR.

All three credentials are injected by the proxy; none are in the sandbox.
"""

import os
import time

from common import ANTHROPIC_RULE, GITHUB_API_RULE, GITHUB_GIT_RULE, banner, claude, client, create_sandbox, require_env, run

require_env("GITHUB_REPO")
REPO = os.environ["GITHUB_REPO"]
BRANCH = f"langsmith-demo-agent-{int(time.time())}"

PROMPT = f"""Clone https://github.com/{REPO}.git into ./repo with git.
Create a branch named {BRANCH}. Add a file docs/langsmith-demo.md with two sentences explaining
what an egress proxy that injects credentials does. Commit it, push the branch, then open a pull
request with `gh pr create --repo {REPO} --fill`. Print only the pull request URL as your final line."""

sbx = create_sandbox("05", rules=[ANTHROPIC_RULE, GITHUB_API_RULE, GITHUB_GIT_RULE])
try:
    run(sbx, "git config --global user.name 'my2cents demo' && git config --global user.email demo@my2cents.ai")

    banner("Claude Code: clone, commit, push, open PR")
    t0 = time.perf_counter()
    # LangSmith sandboxes run as root, and Claude Code refuses to skip permissions as root unless
    # it's told it is inside a sandbox.
    result = claude(sbx, PROMPT, flags="--dangerously-skip-permissions --max-turns 30", env="IS_SANDBOX=1")
    print(f"is_error={result['is_error']}  turns={result['num_turns']}  cost=${result['total_cost_usd']:.4f}")
    print(f"wall={time.perf_counter() - t0:.0f}s")
    print(f"result: {result['result']}")
finally:
    client.delete_sandbox(sbx.name)
