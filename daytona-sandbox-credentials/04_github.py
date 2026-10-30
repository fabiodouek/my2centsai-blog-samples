"""Test 3: git and gh against GitHub with no token in the sandbox.

gh sends GH_TOKEN as Bearer to api.github.com. git speaks Basic auth to github.com, and Daytona only
swaps text, it can't base64-encode, so git sends a pre-encoded secret through http.extraHeader.
"""

import os
import time

from common import GITHUB_ENV, banner, configure_git, create_sandbox, require_env, run

require_env("GITHUB_REPO")
REPO = os.environ["GITHUB_REPO"]
BRANCH = f"daytona-demo-git-{int(time.time())}"

sbx = create_sandbox("04", secrets=GITHUB_ENV)
try:
    banner("What the sandbox has")
    print(run(sbx, "echo GH_TOKEN=$GH_TOKEN GH_BASIC=$GH_BASIC; gh --version | head -1"))

    banner("gh api user (placeholder in GH_TOKEN, swapped on api.github.com)")
    print(run(sbx, "gh api user --jq .login"))

    banner("git clone without the extraHeader: no credential at all")
    print(run(sbx, f"GIT_TERMINAL_PROMPT=0 git clone -q https://github.com/{REPO}.git nocreds 2>&1 | tail -1"))

    banner(f"git clone {REPO} with Authorization: Basic $GH_BASIC")
    configure_git(sbx)
    print(run(sbx, "git config --global --get-regexp 'http.*extraheader'"))
    print(run(sbx, f"git clone -q https://github.com/{REPO}.git repo && git -C repo log --oneline -1"))

    banner(f"git push to branch {BRANCH}")
    print(
        run(
            sbx,
            f"cd repo && git checkout -q -b {BRANCH} && date -u > daytona-demo.txt && git add daytona-demo.txt"
            f" && git commit -qm 'daytona secrets demo' && git push -q origin {BRANCH} 2>&1"
            f" && git ls-remote --heads origin {BRANCH}",
        )
    )

    banner("The remote URL and git config hold only placeholders")
    print(run(sbx, "git -C repo remote get-url origin; grep -h -i -A1 extraheader ~/.gitconfig"))
finally:
    sbx.delete()
