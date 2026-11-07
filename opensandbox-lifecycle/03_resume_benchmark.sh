#!/usr/bin/env bash
# The project's own resume benchmark against this cluster: tests/python/benchmarks/kubernetes_resume.py
# from the release-1.2.0 tag (../opensandbox-eks/04_install.sh fetched it into .src/OpenSandbox).
# Its guide is docs/guides/kubernetes-resume-benchmark.md in the same checkout.
#
# Each trial creates a sandbox, writes a marker file outside any volume, pauses it, waits until
# its pod is gone, resumes it and reads the marker back with a command; then it deletes the
# sandbox. The runner times the resume part from the resume call:
#   api_ack_ms    the resume API answered (accepted, not ready)
#   running_ms    the first GET that says Running
#   ready_ms      a fresh Sandbox.connect passed execd's health check
#   command_ms    the marker came back through a command: the time a user waits
# and reports p50/p95 over the successful measured trials (WARMUPS are not counted).
#
# The runner is run as published, with this directory's .venv (opensandbox==1.2.0, the same SDK
# version as the tag), from its own directory (it records the checkout's git revision). Its
# sandboxes carry the metadata benchmark=kubernetes-rootfs-resume (not ours); it deletes each
# one itself, and 99_cleanup.py removes any an interrupted run leaves. Each trial pushes one
# snapshot image to opensandbox-eks/snapshots (the runner's sandboxes have no network policy, so
# no egress sidecar); 99_cleanup.py deletes them.
#
# Writes results/environment.json (the as-built facts the guide asks for; no credentials) and
# results/resume-uncontrolled-<UTC time>.jsonl (one record per trial plus the summary). The API key
# is read from the Secret opensandbox-server-auth into the runner's environment, never printed.
# The runner turns the SDK's retries off (it measures), so a dropped connection fails a trial and,
# if it hits the clean-up, stops the run. The script therefore runs its own port-forward on
# 127.0.0.1:18081 (BENCH_PORT) for the whole run, started again if it exits, instead of sharing
# 18080 with your shell or another script.
# The runner's stderr is merged into the output, without the SDK's "Failed to get sandbox info ...
# not found" log line: the runner polls each deleted sandbox until the server answers 404.
#
#   ./03_resume_benchmark.sh                  30 samples after 3 warmups (about 15 minutes)
#   SAMPLES=5 WARMUPS=1 ./03_resume_benchmark.sh
set -euo pipefail
cd "$(dirname "$0")"

EKS="../opensandbox-eks"
RUNNER_DIR="${EKS}/.src/OpenSandbox/tests/python"
SAMPLES="${SAMPLES:-30}"
WARMUPS="${WARMUPS:-3}"
NS="opensandbox"
SYS_NS="opensandbox-system"
BENCH_PORT="${BENCH_PORT:-18081}"
DOMAIN="127.0.0.1:${BENCH_PORT}"

[[ -f "${EKS}/env.sh" ]] || { echo "run ../opensandbox-eks/01_cluster.sh first" >&2; exit 1; }
[[ -f "${RUNNER_DIR}/benchmarks/kubernetes_resume.py" ]] \
  || { echo "${RUNNER_DIR}/benchmarks/kubernetes_resume.py not found: run ../opensandbox-eks/04_install.sh first" >&2; exit 1; }
for tool in aws kubectl jq curl uv base64; do
  command -v "$tool" >/dev/null || { echo "$tool is not installed (see README.md)" >&2; exit 1; }
done
# shellcheck source=/dev/null
source "${EKS}/env.sh"
kubectl() { command kubectl --context "$KUBE_CONTEXT" "$@"; }
kubectl get --raw /readyz >/dev/null 2>&1 \
  || { echo "cannot reach context '${KUBE_CONTEXT}': is the cluster up? (../opensandbox-eks/08_scale.sh up)" >&2; exit 1; }
expires="$(kubectl -n "$NS" get secret opensandbox-snapshot-ecr -o jsonpath='{.metadata.annotations.opensandbox-lifecycle/token-expires-at}' 2>/dev/null || true)"
[[ -n "$expires" ]] || { echo "no snapshot registry Secret: run ./00_snapshot_config.sh first" >&2; exit 1; }
expires_s="$(date -u -j -f '%Y-%m-%dT%H:%M:%SZ' "$expires" +%s 2>/dev/null || date -u -d "$expires" +%s 2>/dev/null || true)"
[[ "$expires_s" =~ ^[0-9]+$ ]] || { echo "cannot read the token expiry '${expires}' on opensandbox-snapshot-ecr: run ./00_snapshot_config.sh again" >&2; exit 1; }
left=$(( expires_s - $(date -u +%s) ))
(( left > 3600 )) || { echo "the ECR token in opensandbox-snapshot-ecr expires at ${expires}: run ./00_snapshot_config.sh first" >&2; exit 1; }

uv sync --quiet
PY="$PWD/.venv/bin/python"
mkdir -p results logs
RESULTS="$PWD/results"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
OUTPUT="${RESULTS}/resume-uncontrolled-${stamp}.jsonl"

# --- a port-forward of its own ------------------------------------------------------------
# The body is captured first: `curl | grep -q` under pipefail can fail when grep exits before curl writes.
healthy() { local body; body="$(curl -fsS -m 2 "http://${DOMAIN}/health" 2>/dev/null || true)"; [[ "$body" == *'"healthy"'* ]]; }
if (exec 3<>"/dev/tcp/127.0.0.1/${BENCH_PORT}") 2>/dev/null; then
  echo "something already listens on 127.0.0.1:${BENCH_PORT}: stop it, or set BENCH_PORT to a free port" >&2
  exit 1
fi
keeper_flag="$(mktemp)"
keep_forward() {
  while [[ -e "$keeper_flag" ]]; do
    command kubectl --context "$KUBE_CONTEXT" -n "$SYS_NS" port-forward svc/opensandbox-server "${BENCH_PORT}:80" \
      --address 127.0.0.1 >>logs/port-forward-bench.log 2>&1 || true
    sleep 1   # kubectl exits when its pod goes away or a stream breaks: start it again
  done
}
keep_forward &
keeper_pid=$!
# shellcheck disable=SC2329  # called by the EXIT trap
cleanup() {
  rm -f "$keeper_flag"
  pkill -P "$keeper_pid" kubectl 2>/dev/null || true
  kill "$keeper_pid" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT TERM
for _ in $(seq 1 90); do healthy && break; sleep 0.5; done
healthy || { echo "Server not reachable at ${DOMAIN}: see logs/port-forward-bench.log" >&2; exit 1; }

# --- what the guide asks to record ---------------------------------------------------------
digest="$(aws ecr describe-images --region "$AWS_REGION" --repository-name "${ECR_PREFIX}/python" \
  --image-ids imageTag=3.12-slim --query 'imageDetails[0].imageDigest' --output text)"
IMAGE="${ECR_REGISTRY}/${ECR_PREFIX}/python:3.12-slim@${digest}"
image_id() {  # the digest a running pod's container was pulled at
  kubectl -n "$SYS_NS" get pods -l "$1" -o jsonpath='{.items[0].status.containerStatuses[0].imageID}' | sed 's/.*@//'
}
runtime="$(./server_config.py --show --json | jq -r .runtime)"
nodes="$(kubectl get nodes -o json | jq -r '[.items[] | "\(.metadata.labels["node.kubernetes.io/instance-type"]) \(.status.nodeInfo.osImage), kernel \(.status.nodeInfo.kernelVersion), \(.status.nodeInfo.containerRuntimeVersion), \(.status.capacity.cpu) vCPU, \(.status.capacity.memory | rtrimstr("Ki") | tonumber / 1048576 * 10 | floor / 10) GiB"] | group_by(.) | map("\(length) x \(.[0])") | join("; ")')"
others="$(kubectl -n "$NS" get batchsandboxes.sandbox.opensandbox.io --no-headers 2>/dev/null | wc -l | tr -d ' ')"
args="$(kubectl -n "$SYS_NS" get deploy opensandbox-controller-manager -o json \
  | jq -r '[.spec.template.spec.containers[0].args[] | select(test("snapshot|resume-pull|commit"))] | join(" ")' \
  | sed "s|${ECR_REGISTRY}|<registry>|g")"
jq -n --arg server "server release-1.2.0 @ $(image_id app.kubernetes.io/instance=opensandbox-server)" \
      --arg controller "controller release-1.2.0 @ $(image_id control-plane=controller-manager)" \
      --arg execd "${ECR_PREFIX}/execd:release-1.2.0 (mirrored from docker.io/opensandbox/execd:release-1.2.0)" \
      --arg committer "${ECR_PREFIX}/image-committer:release-1.2.0 (mirrored from docker.io)" \
      --arg runtime "$( [[ $runtime == runc ]] && echo 'runc (default)' || echo "$runtime" )" \
      --arg nodes "$nodes; EKS 1.37 managed node group, gp3 100 GiB root volume" \
      --arg args "$args" --arg others "$others" '{
  server_revision: $server, controller_image: $controller, execd_image: $execd, image_committer_image: $committer,
  runtime_class: $runtime, node_resources: $nodes,
  registry: "Amazon ECR private repository opensandbox-eks/snapshots in the same Region (us-east-1); nodes reach it through a NAT gateway (API) and an S3 gateway endpoint (layers)",
  snapshot_configuration: $args,
  cache_evidence: "Uncontrolled; no cache intervention",
  background_load: ("BatchSandboxes in the namespace when the run started: " + $others + "; other parts of the series may create sandboxes during the run")
}' >"${RESULTS}/environment.json"
echo "image       ${ECR_PREFIX}/python:3.12-slim@${digest:0:19}..."
echo "runtime     ${runtime}; nodes: ${nodes}"
echo "trials      ${WARMUPS} warmups + ${SAMPLES} samples, server proxy through ${DOMAIN}, output results/$(basename "$OUTPUT")"
echo

# --- the run -----------------------------------------------------------------------------
started=$(date +%s)
key="$(kubectl -n "$SYS_NS" get secret opensandbox-server-auth -o jsonpath='{.data.api-key}' | base64 -d)"
set +e
(
  cd "$RUNNER_DIR"
  OPEN_SANDBOX_DOMAIN="$DOMAIN" OPEN_SANDBOX_API_KEY="$key" "$PY" benchmarks/kubernetes_resume.py \
    --image "$IMAGE" --namespace "$NS" --context "$KUBE_CONTEXT" --server-proxy \
    --environment "${RESULTS}/environment.json" --scenario uncontrolled \
    --warmups "$WARMUPS" --samples "$SAMPLES" --output "$OUTPUT"
) 2>&1 | grep --line-buffered -v '^Failed to get sandbox info .* not found' | sed "s|${ECR_REGISTRY}|<registry>|g"
rc=${PIPESTATUS[0]}
set -e
unset key

# The runner deletes each trial's sandbox itself; when a clean-up failed it stops and names the
# sandbox. Delete any sandbox of this run that is still listed, by the ids in its own results file
# (and, for a create whose answer was lost, by its benchmark_run metadata). Nothing else.
OSB_DOMAIN="$DOMAIN" "$PY" - "$OUTPUT" <<'EOF_PY' || echo "  could not check for sandboxes this run left (see above); 99_cleanup.py does not delete them" >&2
import json, sys
from pathlib import Path
from opensandbox.models.sandboxes import SandboxFilter
from opensandbox.sync import SandboxManagerSync
from common import connection, delete_all_sandboxes, ensure_port_forward
ensure_port_forward()
trials = [json.loads(l) for l in Path(sys.argv[1]).read_text().splitlines() if l.strip()] if Path(sys.argv[1]).is_file() else []
trials = [t for t in trials if t.get("type") == "trial"]
with SandboxManagerSync.create(connection_config=connection("proxy")) as manager:
    listed = {i.id for i in manager.list_sandbox_infos(
        SandboxFilter(metadata={"benchmark": "kubernetes-rootfs-resume"}, page_size=200)).sandbox_infos}
    ours = [t["sandbox_id"] for t in trials if t.get("sandbox_id") in listed]
    for sandbox_id in ours:
        manager.kill_sandbox(sandbox_id)
lost = [t["run_id"] for t in trials if not t.get("sandbox_id") and t.get("run_id")]
gone = sum(delete_all_sandboxes(metadata={"benchmark_run": run_id}) for run_id in lost)
print(f"  sandboxes of this run still listed after the runner: {len(ours) + gone} (deleted)")
EOF_PY
took=$(( $(date +%s) - started ))

# --- the summary ---------------------------------------------------------------------------
printf '\n\033[1m== Summary\033[0m (run took %dm %02ds)\n' $((took / 60)) $((took % 60))
if [[ -s "$OUTPUT" ]]; then
  jq -r 'select(.type == "summary") |
    "  completed_requested_trials: \(.completed_requested_trials)   measured \(.measured_trials), successful \(.successful_trials), failed \(.failed_trials), warmup failures \(.warmup_failures), cleanup failures \(.cleanup_failures)",
    (.timings | to_entries[] | "  \(.key | . + " " * (12 - length))  p50 \(.value.p50_ms // "null" | if type == "number" then (. | floor | tostring) + " ms" else . end)   p95 \(.value.p95_ms // "null" | if type == "number" then (. | floor | tostring) + " ms" else . end)")' "$OUTPUT"
  jq -rs '[.[] | select(.type == "trial" and .warmup == false and .success)] as $t
    | if ($t | length) == 0 then empty else
      "  command_ms over \($t | length) trials: min \([$t[].command_ms] | min | floor) ms, max \([$t[].command_ms] | max | floor) ms; restored on \([$t[].restored_pods[0].node] | group_by(.) | map("\(length) x \(.[0] | split(".")[0])") | join(", "))"
    end' "$OUTPUT"
fi
exit "$rc"
