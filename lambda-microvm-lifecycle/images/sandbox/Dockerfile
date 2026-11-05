# The container your code runs in. The MicroVM OS (Amazon Linux 2023) comes from the
# managed base image passed as --base-image-arn; this FROM only sets the container layer.
FROM public.ecr.aws/docker/library/python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl dnsutils iproute2 procps git ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir websockets==15.0.1 boto3==1.43.123

WORKDIR /app
COPY agent.py .
EXPOSE 8080 8081 9001
CMD ["python", "-u", "agent.py"]
