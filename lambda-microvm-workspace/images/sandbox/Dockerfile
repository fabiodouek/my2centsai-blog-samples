# The workspace sandbox: part 2's image (Claude Code, gh, git, the AWS CLI, no credentials, trusts the
# iron-proxy demo CA) plus tmux, a shell setup and Claude Code pre-configured so the interactive TUI
# starts without onboarding, theme, API key or trust prompts. ca.crt and the approved placeholder key in
# claude.json are filled in by scripts/build_sandbox_image.py from part 2's Terraform outputs.
FROM public.ecr.aws/docker/library/node:22-bookworm-slim

ARG CLAUDE_CODE=2.1.317
ARG GH=2.103.3
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-boto3 curl ca-certificates git jq dnsutils \
      iproute2 procps unzip less tmux ncurses-term vim-tiny nano htop tini \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE} \
 && curl -sL https://github.com/cli/cli/releases/download/v${GH}/gh_${GH}_linux_arm64.tar.gz \
    | tar -xz -C /usr/local --strip-components=1 \
 && curl -sL https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip -o /tmp/aws.zip \
 && unzip -q /tmp/aws.zip -d /tmp && /tmp/aws/install && rm -rf /tmp/aws /tmp/aws.zip

COPY ca.crt /usr/local/share/ca-certificates/iron-proxy-demo.crt
RUN update-ca-certificates \
 && git config --system user.name "my2cents demo" \
 && git config --system user.email "demo@my2cents.ai" \
 && git config --system init.defaultBranch main \
 && git config --system credential.helper '!f() { echo username=x-access-token; echo "password=$GH_TOKEN"; }; f'

# Node adds to its bundled roots; the others replace theirs with the system bundle (which now has the CA).
# IS_SANDBOX=1 lets Claude Code run in bypass permissions mode as root. bashrc repeats these for the shell.
ENV NODE_EXTRA_CA_CERTS=/usr/local/share/ca-certificates/iron-proxy-demo.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    AWS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    IS_SANDBOX=1 \
    DISABLE_AUTOUPDATER=1 \
    LANG=C.UTF-8

# Shell, tmux and Claude Code configuration for root
COPY bashrc /root/.bashrc
COPY profile /root/.profile
COPY tmux.conf /root/.tmux.conf
COPY motd /etc/motd
COPY claude.json /root/.claude.json
COPY settings.json /root/.claude/settings.json
RUN mkdir -p /root/work && chmod 600 /root/.claude.json

WORKDIR /app
COPY agent.py warm.sh ./
EXPOSE 8080 9001
# tini as PID 1 reaps orphaned processes (tmux's daemonised server, killed children of `timeout`...), which
# the agent would otherwise leave as zombies
CMD ["/usr/bin/tini", "--", "python3", "-u", "agent.py"]
