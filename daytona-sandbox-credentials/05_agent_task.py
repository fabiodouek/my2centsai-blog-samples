"""Test 4: Claude Code on Bedrock does a real task end to end: clone, change, push, open a PR.

The Bedrock key and both GitHub credentials are Daytona Secrets; the sandbox holds placeholders only.
"""

import os
import time

from common import BEDROCK_ENV, GITHUB_ENV, banner, bedrock_env, claude, configure_git, create_sandbox, require_env

require_env("GITHUB_REPO")
REPO = os.environ["GITHUB_REPO"]
BRANCH = f"daytona-demo-agent-{int(time.time())}"

PROMPT = f"""Clone https://github.com/{REPO}.git into ./repo with git.
Create a branch named {BRANCH}. Add a file docs/daytona-demo.md with two sentences explaining
what an egress proxy that swaps placeholder secrets for real ones does. Commit it, push the branch,
then open a pull request with `gh pr create --repo {REPO} --fill`. Print only the pull request URL
as your final line."""

sbx = create_sandbox("05", secrets={**BEDROCK_ENV, **GITHUB_ENV}, env_vars=bedrock_env())
try:
    configure_git(sbx)

    banner("Claude Code on Bedrock: clone, commit, push, open PR")
    t0 = time.perf_counter()
    result = claude(sbx, PROMPT, flags="--dangerously-skip-permissions --max-turns 30")
    print(f"is_error={result['is_error']}  turns={result['num_turns']}  cost=${result['total_cost_usd']:.4f}")
    print(f"wall={time.perf_counter() - t0:.0f}s")
    print(f"result: {result['result']}")
finally:
    sbx.delete()
