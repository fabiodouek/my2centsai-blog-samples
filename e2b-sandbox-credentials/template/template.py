"""Claude Code + git + GitHub CLI on the E2B base image. No credentials are baked in."""

from e2b import Template

GH_REPO = (
    "curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg"
    " -o /etc/apt/keyrings/githubcli-archive-keyring.gpg"
    ' && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg]'
    ' https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list'
)

template = (
    Template()
    .from_base_image()
    .run_cmd(f"mkdir -p /etc/apt/keyrings && {GH_REPO}", user="root")
    .apt_install(["gh", "git"])
    .npm_install(["@anthropic-ai/claude-code"], g=True)
    .set_envs(
        {
            # Claude Code only needs api.anthropic.com; everything else is denied at the network layer.
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
)
