#!/usr/bin/env bash
# Deploy the echo server (manifests/echo.yaml) into namespace echo, from the series' workload
# image in ECR. Safe to re-run.
source "$(dirname "$0")/lib.sh"
IMAGE="${ECR_REGISTRY}/${ECR_PREFIX}/workload:0.1.6"
sed "s|\${IMAGE}|${IMAGE}|" "${HERE}/manifests/echo.yaml" | kubectl apply -f - >/dev/null
kubectl -n echo rollout status deployment/echo --timeout=3m >/dev/null && echo "echo server ready: http://${ECHO_HOST}/"
