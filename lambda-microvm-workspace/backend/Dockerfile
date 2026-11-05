# Workspace API (FastAPI + uvicorn) for ECS Fargate ARM64. Build: docker buildx build --platform linux/arm64 .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH

RUN pip install --no-cache-dir uv==0.10.12 && useradd --system --uid 10001 --home /app app
WORKDIR /app

# Dependencies first (cached layer), from the lock file
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project && rm -rf /root/.cache

COPY app ./app
# boto3 client -> signing name and us-east-1 endpoint, from the botocore just installed (see app/aws_endpoints.py)
RUN python -m app.aws_endpoints > app/data/aws_endpoints.json
USER app
EXPOSE 8000

# WebSocket pings every 20 s keep CloudFront/ALB from idling out quiet sockets (the app also sends heartbeats).
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--ws-ping-interval", "20", "--ws-ping-timeout", "20", "--timeout-keep-alive", "65", "--no-server-header"]
