# Workload image for the part 1 sandboxes: the NVIDIA Ubuntu base OpenShell 0.1.x uses by
# default, pinned by digest because the 24.04 tag moves, plus the tools the tests call.
FROM nvcr.io/nvidia/base/ubuntu:24.04@sha256:d9461f428eea54e553add3a5158a0f245c6e1ea73efc59fff8e98e3d4a38e378

USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl python3 git jq \
 && rm -rf /var/lib/apt/lists/*

# OpenShell refuses to start a workload as UID or GID 0 ("workload identity must not contain
# UID or GID zero"). Run as the image's existing ubuntu user.
USER 1000:1000
