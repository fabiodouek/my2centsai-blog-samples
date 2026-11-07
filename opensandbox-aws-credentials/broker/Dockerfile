# The part 5 credential broker (broker.py). 02_broker.sh builds it for linux/amd64 on the
# python:3.12-slim copy in your ECR registry (../opensandbox-eks/03_images.sh put it there)
# and pushes it as opensandbox-eks/broker:release-1.2.0. The default base is the same image
# on Amazon ECR Public, for a local build:
#   docker buildx build --platform linux/amd64 --load -t osb-broker broker/
ARG BASE_IMAGE=public.ecr.aws/docker/library/python:3.12-slim
FROM ${BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY broker.py sandbox_permissions.json vending_actions.json ./

# Not root. The Deployment also makes the root filesystem read-only.
USER 10001:10001
ENTRYPOINT ["python", "/app/broker.py"]
CMD ["serve"]
