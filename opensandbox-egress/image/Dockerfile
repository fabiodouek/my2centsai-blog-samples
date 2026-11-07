# The workload image for part 4: the python:3.12-slim image from ../opensandbox-eks plus the clients the
# scripts compare (curl, git, Node, the AWS CLI v2, Python requests). It is built for linux/amd64 (the
# m7i nodes) and pushed to ECR by ../00_image.sh. The base comes from Amazon ECR Public, the same copy
# 03_images.sh mirrors, so nothing is pulled through Docker Hub.
FROM public.ecr.aws/docker/library/python:3.12-slim

ARG AWSCLI_VERSION=2.39.3
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git nodejs unzip procps iproute2 \
 && curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64-${AWSCLI_VERSION}.zip" -o /tmp/awscli.zip \
 && unzip -q /tmp/awscli.zip -d /tmp \
 && /tmp/aws/install \
 && rm -rf /tmp/aws /tmp/awscli.zip /var/lib/apt/lists/* \
 && pip install --no-cache-dir requests
