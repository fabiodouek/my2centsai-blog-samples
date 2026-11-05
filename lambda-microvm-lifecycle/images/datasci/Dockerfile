# Same agent as images/sandbox, plus a data-science stack baked into the snapshot.
# 08_many_from_one.py compares this with `pip install` at runtime.
FROM public.ecr.aws/docker/library/python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl dnsutils iproute2 procps git ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir websockets==15.0.1 boto3==1.43.123 \
      pandas==2.3.3 numpy==2.3.4 scikit-learn==1.7.2 matplotlib==3.10.7

WORKDIR /app
# common.build_image() copies images/sandbox/agent.py into the build zip
COPY agent.py .
EXPOSE 8080 8081 9001
CMD ["python", "-u", "agent.py"]
