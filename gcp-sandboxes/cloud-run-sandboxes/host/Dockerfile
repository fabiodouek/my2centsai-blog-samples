FROM python:3.12-slim
# Sandboxes see the host's root filesystem (read-only), so every tool a sandbox
# should be able to use has to be installed here.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl procps iproute2 openssl util-linux sudo \
 && rm -rf /var/lib/apt/lists/*
# A root-owned, mode 0600 file with dummy content (test 3: can a sandbox read it?)
RUN echo "m2c-root-only-canary" > /etc/m2c-0600 && chmod 600 /etc/m2c-0600
COPY server.py /app/server.py
CMD ["python3", "/app/server.py"]
