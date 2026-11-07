"""Real services through the Credential Vault: the sandbox holds placeholders, the sidecar holds the keys.

Three bindings, two real credentials (read from the secrets .env with read_env_key; never printed):

  github-api  bearer   api.github.com        curl -H "Authorization: Bearer <placeholder>" arrives authenticated
  github-git  basic    github.com            git clone / git push over HTTPS authenticate (user x-access-token)
  anthropic   apiKey   api.anthropic.com     header x-api-key; a Messages API call returns a completion

The sandbox environment only ever contains the placeholder strings. The script then checks that the real
values are in none of the sandbox's output. The git push goes to a throwaway branch osb-test-<run id> of
the test repo (GITHUB_REPO), and that branch is deleted again; the default branch is never touched.

Needs ./00_image.sh and the secrets .env (see .env.example). Kills only its own sandboxes.
"""

import json
import logging
import sys
import sys
import uuid

from common import (
    Checks,
    basic_credential,
    binding,
    safe_cleanup,
    create_sandbox,
    credential,
    deny_policy,
    ensure_port_forward,
    heading,
    note,
    read_env_key,
    require_server,
    scrub,
    sh,
    show,
)

SCRIPT = "04_vault_services"
RUN = uuid.uuid4().hex[:10]
BRANCH = f"osb-test-{RUN}"
MODEL = "claude-haiku-4-5"  # any model your key can call
PLACEHOLDER_GITHUB = "osb-placeholder-github"
PLACEHOLDER_ANTHROPIC = "osb-placeholder-anthropic"
checks = Checks()

github_token = read_env_key("GITHUB_TOKEN")
repo = read_env_key("GITHUB_REPO", secret=False)
anthropic_key = read_env_key("ANTHROPIC_API_KEY")
real_values = [github_token, anthropic_key]

ensure_port_forward()
require_server()
# Real values travel in this script's requests. The log records are scrubbed (common.py), and the SDK's own
# logger stays quiet so that no traceback or request detail can carry one.
logging.getLogger("opensandbox").setLevel(logging.CRITICAL)
sb = None
pushed = False


def remove_branch() -> bool:
    """Delete the throwaway branch on the remote and verify it is gone, from the sandbox (the basic binding authenticates)."""
    global pushed
    deleted = sh(sb, f"cd /tmp/work && git push -q origin --delete {BRANCH}", timeout=120)
    listing = sh(sb, f"cd /tmp/work && git ls-remote --heads origin {BRANCH}", timeout=120)
    gone = listing.rc == 0 and BRANCH not in listing.text
    print(f"  git push --delete rc {deleted.rc}; git ls-remote rc {listing.rc}, {'branch absent' if gone else 'BRANCH STILL THERE or ls-remote failed'}")
    if gone:
        pushed = False
    return gone


try:
    policy = deny_policy("api.github.com", "github.com", "api.anthropic.com")
    sb = create_sandbox(SCRIPT, RUN, policy=policy, proxy=True,
                        env={"GITHUB_TOKEN": PLACEHOLDER_GITHUB, "ANTHROPIC_API_KEY": PLACEHOLDER_ANTHROPIC,
                             "GIT_TERMINAL_PROMPT": "0"})
    state = sb.credential_vault.create(
        credentials=[
            credential("github-token", github_token),
            credential("github-basic", basic_credential("x-access-token", github_token)),
            credential("anthropic-key", anthropic_key),
        ],
        bindings=[
            binding("github-api", ["api.github.com"], {"type": "bearer", "credential": "github-token"}),
            binding("github-git", ["github.com"], {"type": "basic", "credential": "github-basic"}),
            binding("anthropic", ["api.anthropic.com"], {"type": "apiKey", "name": "x-api-key", "credential": "anthropic-key"}),
        ],
    )
    print(f"  sandbox {sb.id}; vault revision {state.revision}: {[c.name for c in state.credentials]} / {[b.name for b in state.bindings]}")
    print("  (values are write-only: the sanitized state above has names and revisions, never a value)")

    heading("what the sandbox holds")
    out = sh(sb, "env | grep -E 'GITHUB|ANTHROPIC' | sort")
    show(out.text)
    checks.check("the sandbox env has only the placeholders", not any(v in out.text for v in real_values)
                 and PLACEHOLDER_GITHUB in out.text and PLACEHOLDER_ANTHROPIC in out.text)

    heading("bearer: GitHub API with the placeholder")
    out = sh(sb, 'curl -sS -m 20 -H "Authorization: Bearer $GITHUB_TOKEN" -H "User-Agent: osb-example" https://api.github.com/user '
                 '| python3 -c "import json,sys; d=json.load(sys.stdin); print(\'authenticated as\', d.get(\'login\') or d)"', timeout=40)
    show(out.text, limit=3)
    checks.check("api.github.com sees an authenticated user (bearer injected over the placeholder)", "authenticated as" in out.text and "Bad credentials" not in out.text, out.text[:80])

    heading("basic: git clone and push to the test repo, throwaway branch " + BRANCH)
    url = f"https://github.com/{repo}.git"
    clone = sh(sb, f"cd /tmp && git clone --depth 1 -q '{url}' work && cd work && git log --oneline | head -1", timeout=120)
    show(clone.text, limit=4)
    checks.check("git clone of the private repo authenticates (no credentials in the sandbox)", clone.rc == 0, clone.text[:120])
    if clone.rc == 0:
        pushed = True  # from here on the branch may exist on the remote; finally removes it if the checks below do not
        push = sh(sb, f"cd /tmp/work && git checkout -q -b {BRANCH} && echo 'vault example {RUN}' > osb-example.txt && git add osb-example.txt "
                      f"&& git -c user.name=osb-example -c user.email=osb-example@example.invalid commit -q -m 'osb example {RUN}' "
                      f"&& git push -q origin {BRANCH} && git ls-remote --heads origin {BRANCH}", timeout=120)
        show(push.text, limit=4)
        checks.check("git push to the throwaway branch succeeds", push.rc == 0 and BRANCH in push.text, push.text[:120])
        checks.check("the throwaway branch is deleted from the remote and absent from ls-remote", remove_branch())
        remotes = sh(sb, "cd /tmp/work && git remote -v | head -1; cat .git/config | grep -ci 'token\\|extraheader'")
        show(remotes.text, limit=3)

    heading("apiKey: Anthropic Messages API with the placeholder")
    body = json.dumps({"model": MODEL, "max_tokens": 24, "messages": [{"role": "user", "content": "Reply with the single word: ready"}]})
    out = sh(sb, "curl -sS -m 40 https://api.anthropic.com/v1/messages -H 'content-type: application/json' "
                 "-H 'anthropic-version: 2023-06-01' -H \"x-api-key: $ANTHROPIC_API_KEY\" "
                 f"-d '{body}' | python3 -c \"import json,sys; d=json.load(sys.stdin); "
                 "print((d.get('content') or [{}])[0].get('text') or d.get('error') or d)\"", timeout=70)
    show(out.text, limit=3)
    checks.check("api.anthropic.com returns a completion", out.rc == 0 and "authentication" not in out.text.lower() and "error" not in out.text.lower(), out.text[:100])

    heading("the real values are nowhere in the sandbox")
    dump = sh(sb, "env; cat /proc/*/environ 2>/dev/null | tr '\\0' '\\n'; cat /tmp/work/.git/config ~/.gitconfig ~/.git-credentials 2>/dev/null; true", timeout=60)
    checks.check("env, /proc/*/environ and git config hold no real token or key", not any(v in dump.text for v in real_values))
    note("the sidecar redacts injected values in response headers; response bodies are not rewritten.")
except Exception as error:
    checks.check(f"script ran to the end ({type(error).__name__})", False, scrub(str(error))[:300])
finally:
    heading("clean up")
    if pushed and sb is not None:  # an exception or a failed check left the branch on the remote: remove it now
        try:
            print(f"  removing the throwaway branch {BRANCH} left on the remote")
            if not remove_branch():
                print(f"  COULD NOT REMOVE {BRANCH} from the test repo: delete it by hand", file=sys.stderr)
        except BaseException as error:
            print(f"  COULD NOT REMOVE {BRANCH} from the test repo ({type(error).__name__}): delete it by hand", file=sys.stderr)
    safe_cleanup(RUN)

checks.finish()
