# Cloudflare Sandbox base image — provides the sandbox runtime that the
# Worker SDK (`@cloudflare/sandbox`) talks to over port 3000 (exec, terminals,
# file I/O, etc.). Do NOT override ENTRYPOINT or the runtime won't start.
FROM docker.io/cloudflare/sandbox:0.10.2-python

ENV PATH="/usr/local/bin:/usr/bin:/bin:/home/claude/.npm-global/bin:/home/user/.npm-global/bin:${PATH}"

# ---------------------------------------------------------------------------
# System utilities & build essentials (kept minimal — sandbox base ships most)
# ---------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    bc \
    ca-certificates \
    curl \
    file \
    git \
    jq \
    less \
    lsof \
    nano \
    netcat-openbsd \
    ripgrep \
    sudo \
    tmux \
    unzip \
    vim \
    wget \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Anthropic agent runner CLI (`ant`). We keep it on PATH and start it via
# `sandbox.startProcess(...)` from the worker after dispatch — running it as
# the entrypoint would replace the sandbox runtime. This build also resolves
# the session's agent skills and mounts them under `/workspace/skills/<name>/`
# before any tool runs (see the 0512 self-hosted integration guide).
# ---------------------------------------------------------------------------
ARG ANT_SHA=0058203228b4165d10d29ba225b8ff3053f6ac83
ADD https://app.stainless.com/pkg/s/anthropic-cli/${ANT_SHA}/dist.zip /tmp/ant.zip

RUN DIR=$([ "$TARGETARCH" = "arm64" ] && echo linux_linux_arm64_v8.0 || echo linux_linux_amd64_v1) \
 && unzip -oj /tmp/ant.zip "${DIR}/ant" -d /usr/local/bin \
 && chmod +x /usr/local/bin/ant && rm /tmp/ant.zip

WORKDIR /workspace
RUN mkdir -p /workspace

# The dashboard PTY terminal goes through @cloudflare/sandbox's `terminal()`
# RPC, whose `PtyOptions` type doesn't expose a `cwd` (only cols/rows/shell).
# Without that, the shell spawned by the in-container runtime lands in its
# own default cwd (typically $HOME = /root) instead of /workspace — so files
# the agent writes to /workspace look "missing" from the terminal. Force
# both interactive and login shells to land in /workspace so the terminal
# view matches what the agent sees.
RUN printf '\n# Land in the agent workspace by default.\ncd /workspace 2>/dev/null || true\n' >> /root/.bashrc \
 && printf '# Land in the agent workspace by default.\ncd /workspace 2>/dev/null || true\n' > /etc/profile.d/cd-workspace.sh \
 && chmod 0644 /etc/profile.d/cd-workspace.sh

# 3000 is the sandbox runtime; 8080 is for user code if it exposes a server.
EXPOSE 3000
EXPOSE 8080
