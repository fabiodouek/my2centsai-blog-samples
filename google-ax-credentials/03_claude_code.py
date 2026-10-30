"""Step 3: Claude Code with a placeholder API key. The real key is added by the
egress gateway on the way to api.anthropic.com."""

from common import PLACEHOLDER, banner, create_task, https, inject, load_env, redact, set_policy, sh, show

NAME = "cred-claude"
REAL = load_env()["ANTHROPIC_API_KEY"]
ENV = {
    "ANTHROPIC_API_KEY": PLACEHOLDER,                      # Claude Code needs a value; this one is fake
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",       # only api.anthropic.com is reachable anyway
}
ASK = "timeout 90 claude -p 'Reply with exactly: injected-ok' --model claude-haiku-5-5 < /dev/null"

banner("the sandbox's view of the key")
create_task(NAME, ENV)
show(NAME, "echo ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY; claude --version")

banner("allow api.anthropic.com, but inject nothing")
set_policy(NAME, [https("api.anthropic.com")])
print(redact(sh(NAME, f"{ASK} 2>&1 | tail -2"), REAL))

banner("now swap x-api-key for the real key")
set_policy(NAME, [inject("api.anthropic.com", "x-api-key", "anthropic", "api-key")])
print(redact(sh(NAME, f"time ({ASK})"), REAL))

banner("streaming works too")
print(redact(sh(NAME, "claude -p 'Count from 1 to 5, one number per line.' --model claude-haiku-5-5 "
                      "--output-format stream-json --verbose < /dev/null | jq -r 'select(.type==\"assistant\") "
                      "| .message.content[]? | select(.type==\"text\") | .text'"), REAL))
