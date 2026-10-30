# AX's own task runner image plus Claude Code, git and gh.
#
# BASE is the ax-task-runner digest from ../../google-ax-eks/.records/source-and-images.txt.
# No credentials are baked in: the sandbox only ever holds placeholders.
ARG BASE
FROM ${BASE}

ARG CLAUDE_CODE_VERSION=2.1.314
ARG GH_VERSION=2.103.3

RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm jq \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && curl -fsSL -o /tmp/gh.deb "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_amd64.deb" \
    && dpkg -i /tmp/gh.deb && rm /tmp/gh.deb

# Node keeps its own CA list; point it at the system bundle, which
# egress-ca.sh extends with the egress gateway's CA at boot.
ENV NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt

COPY egress-ca.sh /usr/local/bin/egress-ca
RUN chmod +x /usr/local/bin/egress-ca
