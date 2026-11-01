# Workload image for every sandbox in this series (parts 2 to 6): the NVIDIA Ubuntu base that
# OpenShell 0.1.x uses by default, pinned by digest, plus the tools the tests call.
# 03_images.sh builds it for linux/amd64 and pushes it to ECR as ${ECR_PREFIX}/workload:0.1.6.
FROM nvcr.io/nvidia/base/ubuntu@sha256:d9461f428eea54e553add3a5158a0f245c6e1ea73efc59fff8e98e3d4a38e378

ARG AWSCLI_VERSION=2.39.3
ARG BOTO3_VERSION=1.43.123

USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl git gh jq python3 python3-pip unzip \
 && rm -rf /var/lib/apt/lists/*

# AWS CLI v2 from AWS's own installer (Ubuntu's package is v1), and boto3 from PyPI: both
# recent enough to upload to S3 with aws-chunked encoding, which part 5 depends on.
RUN curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64-${AWSCLI_VERSION}.zip" -o /tmp/awscli.zip \
 && unzip -q /tmp/awscli.zip -d /tmp \
 && /tmp/aws/install \
 && rm -rf /tmp/aws /tmp/awscli.zip \
 && pip3 install --no-cache-dir --break-system-packages "boto3==${BOTO3_VERSION}"

# OpenShell refuses to start a workload as UID or GID 0. Run as the image's ubuntu user.
USER 1000:1000
