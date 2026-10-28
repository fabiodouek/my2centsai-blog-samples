"""Test 4: git and gh against GitHub with no token in the sandbox.

git speaks Basic auth to github.com; gh speaks Bearer to api.github.com. Each host gets its own rule.
"""

import os
import time

from common import banner, create_sandbox, require_env, run

require_env("GITHUB_REPO")
REPO = os.environ["GITHUB_REPO"]
BRANCH = f"e2b-demo-git-{int(time.time())}"

sbx = create_sandbox("04", anthropic=False)
try:
    banner("What the sandbox has")
    print(run(sbx, "echo GH_TOKEN=$GH_TOKEN; git config --global --list 2>/dev/null || echo 'no git config'"))
    print(run(sbx, "ls ~/.config/gh 2>/dev/null || echo 'no gh config'"))

    banner("gh api user (Bearer injected on api.github.com)")
    print(run(sbx, "gh api user --jq '.login'"))

    banner(f"git clone {REPO} (Basic injected on github.com)")
    print(run(sbx, f"git clone -q https://github.com/{REPO}.git repo && git -C repo log --oneline -1"))

    banner(f"git push to branch {BRANCH}")
    print(
        run(
            sbx,
            "cd repo && git config user.name 'e2b-demo' && git config user.email 'e2b-demo@example.com'"
            f" && git checkout -q -b {BRANCH} && date -u > e2b-demo.txt && git add e2b-demo.txt"
            f" && git commit -qm 'e2b credential injection demo' && git push -q origin {BRANCH} 2>&1"
            f" && git ls-remote --heads origin {BRANCH}",
        )
    )

    banner("The remote URL has no credentials in it")
    print(run(sbx, "git -C repo remote get-url origin"))
finally:
    sbx.kill()
