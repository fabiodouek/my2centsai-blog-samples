"""Test 5: Claude Code does a real task end to end: clone, change, push, open a PR.

All three credentials are injected; none are in the sandbox.
"""

import os
import time

from common import banner, claude, create_sandbox, require_env, run

require_env("GITHUB_REPO")
REPO = os.environ["GITHUB_REPO"]
BRANCH = f"e2b-demo-agent-{int(time.time())}"

PROMPT = f"""Clone https://github.com/{REPO}.git into ./repo with git.
Create a branch named {BRANCH}. Add a file docs/e2b-demo.md with two sentences explaining
what an egress proxy that injects credentials does. Commit it, push the branch, then open a pull
request with `gh pr create --repo {REPO} --fill`. Print only the pull request URL as your final line."""

sbx = create_sandbox("05", timeout=900)
try:
    run(sbx, "git config --global user.name 'e2b-demo' && git config --global user.email 'e2b-demo@example.com'")

    banner("Claude Code: clone, commit, push, open PR")
    t0 = time.perf_counter()
    result = claude(sbx, PROMPT, flags="--dangerously-skip-permissions --max-turns 30")
    print(f"is_error={result['is_error']}  turns={result['num_turns']}  cost=${result['total_cost_usd']:.4f}")
    print(f"wall={time.perf_counter() - t0:.0f}s")
    print(f"result: {result['result']}")
finally:
    sbx.kill()
