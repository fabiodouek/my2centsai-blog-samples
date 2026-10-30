FROM node:22-bookworm
ARG CLAUDE_CODE=2.1.312
ARG GH=2.103.3
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE} \
 && curl -sL https://github.com/cli/cli/releases/download/v${GH}/gh_${GH}_linux_amd64.tar.gz \
    | tar -xz -C /usr/local --strip-components=1 \
 && apt-get update && apt-get install -y --no-install-recommends python3-boto3 jq && rm -rf /var/lib/apt/lists/*
