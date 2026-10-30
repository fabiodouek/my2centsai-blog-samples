"""Test 3: git and gh against GitHub with no token in the sandbox.

gh sends Bearer to api.github.com; git sends Basic to github.com. One rule per host, values from
workspace secrets. The sandbox only has GH_TOKEN=proxy-injected so gh agrees to run.
"""

import os
import time

from common import GITHUB_API_RULE, GITHUB_GIT_RULE, banner, client, create_sandbox, require_env, run

require_env("GITHUB_REPO")
REPO = os.environ["GITHUB_REPO"]
BRANCH = f"langsmith-demo-git-{int(time.time())}"

sbx = create_sandbox("04", rules=[GITHUB_API_RULE, GITHUB_GIT_RULE])
try:
    banner("What the sandbox has")
    print(run(sbx, "echo GH_TOKEN=$GH_TOKEN; git config --global --list 2>/dev/null || echo 'no git config'"))

    banner("gh api user (Bearer injected on api.github.com)")
    print(run(sbx, "gh api user --jq .login"))

    banner(f"git clone {REPO} (Basic injected on github.com)")
    print(run(sbx, f"git clone -q https://github.com/{REPO}.git repo && git -C repo log --oneline -1"))

    banner(f"git push to branch {BRANCH}")
    print(
        run(
            sbx,
            "cd repo && git config user.name 'my2cents demo' && git config user.email demo@my2cents.ai"
            f" && git checkout -q -b {BRANCH} && date -u > langsmith-demo.txt && git add langsmith-demo.txt"
            f" && git commit -qm 'langsmith proxy demo' && git push -q origin {BRANCH} 2>&1"
            f" && git ls-remote --heads origin {BRANCH}",
        )
    )

    banner("The remote URL and git config have no credentials")
    print(run(sbx, "git -C repo remote get-url origin; cat ~/.gitconfig 2>/dev/null; git -C repo config --list | grep -i -E 'extraheader|credential' || echo 'no credential config'"))
finally:
    client.delete_sandbox(sbx.name)
