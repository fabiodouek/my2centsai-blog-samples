# The sandbox: Claude Code, gh, git and the AWS CLI, with no credentials. Each client trusts the
# iron-proxy demo CA (ca.crt is written by 01_setup.py from the Terraform output; it's public).
FROM public.ecr.aws/docker/library/node:22-bookworm-slim

ARG CLAUDE_CODE=2.1.317
ARG GH=2.103.3
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-boto3 curl ca-certificates git jq dnsutils \
      iproute2 procps unzip less \
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
 && git config --system credential.helper '!f() { echo username=x-access-token; echo "password=$GH_TOKEN"; }; f'

# Node adds to its bundled roots; the others replace theirs with the system bundle (which now has the CA)
ENV NODE_EXTRA_CA_CERTS=/usr/local/share/ca-certificates/iron-proxy-demo.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    AWS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    IS_SANDBOX=1

WORKDIR /app
COPY agent.py .
EXPOSE 8080 9001
CMD ["python3", "-u", "agent.py"]
