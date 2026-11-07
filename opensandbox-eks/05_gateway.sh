#!/usr/bin/env bash
# Put the OpenSandbox ingress gateway behind an internet-facing Network Load Balancer, and make
# the server hand out gateway endpoints. Sandbox pods only have private VPC addresses, so this
# is how SDK and CLI calls to a sandbox (commands, files, the PTY WebSocket) reach it from a
# laptop. The server API itself stays behind kubectl port-forward.
#
#  1. The ingress-gateway chart from .src/OpenSandbox (fetched by ./04_install.sh), release
#     opensandbox-ingress-gateway, with the ECR image (values/ingress-gateway.yaml).
#  2. The Service opensandbox-gateway-nlb (manifests/gateway-nlb.yaml): the AWS Load Balancer
#     Controller creates an internet-facing NLB with IP targets and a TCP listener on port 80.
#     loadBalancerSourceRanges is your public IPv4 address as a /32, from checkip.amazonaws.com
#     on every run: if your address changes, run this again. MY_IP=<address> overrides it.
#     The script waits for the NLB to become active, for its security group to allow exactly
#     that /32, for the gateway pods to pass the NLB health check and for the gateway to answer
#     through the NLB, and prints how long each step took.
#  3. values/server-gateway.yaml (gitignored): server.gateway.enabled, the NLB DNS name as
#     server.gateway.host, and header routing. ./04_install.sh then upgrades the server with
#     it, and includes it on every later run, so running 04_install.sh alone keeps the gateway.
#     The server then answers endpoint requests with the NLB address and the header
#     OpenSandbox-Ingress-To: <sandbox-id>-<port>.
#
# Safe to run again: an unchanged gateway release and Service are reported unchanged, and the
# server is only upgraded when the NLB DNS name changed. A new IP only updates the NLB's
# security group. If your IP changed, the EKS API endpoint (01_cluster.sh) refuses you too:
# run ./01_cluster.sh first.
#
# The gateway speaks cleartext HTTP/1.1, and so does this NLB: anyone at the allowed address
# can reach any sandbox whose ID they know. Keep the /32.
set -euo pipefail
cd "$(dirname "$0")"

OSB_VERSION="1.2.0"
NS="opensandbox-system"
RELEASE="opensandbox-ingress-gateway"         # also the name of its Deployment and pods
CHART=".src/OpenSandbox/manifests/charts/ingress-gateway"
NLB_SVC="opensandbox-gateway-nlb"
GATEWAY_VALUES="values/server-gateway.yaml"   # read by 04_install.sh
ROUTE_MODE="header"       # gateway.gatewayRouteMode in values/ingress-gateway.yaml
EXECD_PORT=44772          # execd in every sandbox, for the routing check
HELM_TIMEOUT="${HELM_TIMEOUT:-5m}"
NLB_TIMEOUT="${NLB_TIMEOUT:-900}"   # seconds for the NLB to be active, healthy and answering
LOGS="logs"               # full Helm and 04_install.sh output (gitignored)

for tool in kubectl helm jq envsubst curl aws; do
  command -v "$tool" >/dev/null || { echo "$tool is not installed (see README.md)" >&2; exit 1; }
done
[[ -f env.sh ]] || { echo "env.sh not found: run ./01_cluster.sh first" >&2; exit 1; }
# shellcheck source=/dev/null
source ./env.sh
: "${KUBE_CONTEXT:?env.sh has no KUBE_CONTEXT: run ./01_cluster.sh again}"
: "${ECR_REGISTRY:?env.sh has no ECR_REGISTRY: run ./01_cluster.sh again}"
: "${ECR_PREFIX:?env.sh has no ECR_PREFIX: run ./01_cluster.sh again}"
: "${AWS_REGION:?env.sh has no AWS_REGION: run ./01_cluster.sh again}"
# Every kubectl and helm call names the context, so switching contexts in another terminal
# can't point this script at a different cluster.
kubectl() { command kubectl --context "$KUBE_CONTEXT" "$@"; }
helm() { command helm --kube-context "$KUBE_CONTEXT" "$@"; }
kubectl get --raw /readyz >/dev/null 2>&1 \
  || { echo "cannot reach context '${KUBE_CONTEXT}': run ./01_cluster.sh first (it also opens the EKS API to a new IP)" >&2; exit 1; }
[[ -d "$CHART" ]] || { echo "${CHART} not found: run ./04_install.sh first" >&2; exit 1; }
server_status="$(helm -n "$NS" status opensandbox-server -o json 2>/dev/null | jq -r '.info.status' 2>/dev/null || true)"
[[ "$server_status" == deployed ]] \
  || { echo "release opensandbox-server is ${server_status:-not installed}: run ./04_install.sh first" >&2; exit 1; }
mkdir -p "$LOGS"
RENDERED="$(mktemp -d)"   # values and manifest with the registry and address filled in
trap 'rm -rf "$RENDERED"' EXIT
trap 'exit 130' INT TERM

# --- output and timing helpers -------------------------------------------------
script_start=$(date +%s)
timings=""
phase_name=""
phase_start=0
duration() { local s=$1; if (( s >= 60 )); then printf '%dm %02ds' $((s / 60)) $((s % 60)); else printf '%ds' "$s"; fi; }
phase() {
  phase_name="$1"
  phase_start=$(date +%s)
  printf '\n\033[1m== %s\033[0m\n' "$1"
}
phase_done() {
  local took=$(( $(date +%s) - phase_start ))
  timings+="$(printf '  %-30s %9s' "$phase_name" "$(duration "$took")")"$'\n'
  printf '   took %s\n' "$(duration "$took")"
}
failures=0
ok()   { printf '  ok    %s\n' "$*"; }
warn() { printf '  WARN  %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*"; failures=$((failures + 1)); }
# wait_until SECONDS COMMAND...: run the command every 5 s until it succeeds; 1 on timeout
wait_until() {
  local limit=$1 start
  start=$(date +%s)
  shift
  until "$@"; do
    if (( $(date +%s) - start >= limit )); then return 1; fi
    sleep 5
  done
}
since() { duration $(( $(date +%s) - $1 )); }   # time since an epoch second

# --- 1. Your address ------------------------------------------------------------
phase "your public IP"
# This machine's address as the internet sees it. MY_IP overrides what the NLB allows; the
# detected address then only tells whether this machine should still get through.
detected="$(curl -fsS -m 10 https://checkip.amazonaws.com 2>/dev/null | tr -d '[:space:]' || true)"
[[ "$detected" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || detected=""
if [[ -n "${MY_IP:-}" ]]; then ip_from="MY_IP"; else MY_IP="$detected"; ip_from="checkip.amazonaws.com"; fi
if [[ ! "$MY_IP" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
  echo "could not work out your public IPv4 address (got '${MY_IP}'): set MY_IP and run again" >&2
  exit 1
fi
export MY_IP
echo "  the NLB will accept ${MY_IP}/32 only (from ${ip_from})"
expect_reachable=1
if [[ "$ip_from" == MY_IP && -n "$detected" && "$detected" != "$MY_IP" ]]; then
  expect_reachable=0
  warn "this machine's public address is ${detected}, not ${MY_IP}: it will be shut out of the gateway"
fi
phase_done

# --- 2. The gateway chart -------------------------------------------------------
phase "ingress gateway"
v="$(helm show chart "$CHART" | sed -n 's/^version: //p')"
if [[ "$v" != "$OSB_VERSION" ]]; then echo "chart ingress-gateway is version ${v}, expected ${OSB_VERSION}" >&2; exit 1; fi
# shellcheck disable=SC2016  # envsubst and grep take ${...} literally
envsubst '${ECR_REGISTRY} ${ECR_PREFIX}' <values/ingress-gateway.yaml >"$RENDERED/ingress-gateway.yaml"
# shellcheck disable=SC2016
if grep -n '\${' "$RENDERED/ingress-gateway.yaml" >&2; then echo "values/ingress-gateway.yaml: unfilled \${...} above" >&2; exit 1; fi

gateway_pods() {  # one line per live gateway pod: name uid restarts ready ip node
  kubectl -n "$NS" get pods -l "app.kubernetes.io/name=${RELEASE}" -o json | jq -r '.items[]
    | select(.metadata.deletionTimestamp == null)
    | "\(.metadata.name) \(.metadata.uid) \([.status.containerStatuses[]?.restartCount] | add // 0) \(all(.status.containerStatuses[]?; .ready) and ((.status.containerStatuses // []) | length > 0)) \(.status.podIP // "-") \(.spec.nodeName // "-")"' | sort
}
release_status() {  # "revision status", or nothing if it isn't installed
  helm -n "$NS" status "$RELEASE" -o json 2>/dev/null | jq -r '"\(.version) \(.info.status)"' 2>/dev/null || true
}
before="$(gateway_pods | awk '{ print $2 }')"
read -r prev_rev prev_status <<<"$(release_status)"
if [[ "${prev_status:-}" == pending-* ]]; then
  echo "release ${RELEASE} is stuck in ${prev_status} (an earlier helm run was killed):" >&2
  echo "  helm --kube-context ${KUBE_CONTEXT} -n ${NS} rollback ${RELEASE}   (or uninstall it if it never deployed), then run this again" >&2
  exit 1
fi
if ! helm upgrade --install "$RELEASE" "$CHART" --namespace "$NS" -f "$RENDERED/ingress-gateway.yaml" \
    --wait --timeout "$HELM_TIMEOUT" >"$LOGS/helm-${RELEASE}.log" 2>&1; then
  tail -n 20 "$LOGS/helm-${RELEASE}.log" >&2
  echo "--- gateway pods" >&2
  kubectl -n "$NS" get pods -l "app.kubernetes.io/name=${RELEASE}" -o wide >&2 || true
  kubectl -n "$NS" get events --field-selector involvedObject.kind=Pod \
    -o custom-columns='LAST SEEN:.lastTimestamp,OBJECT:.involvedObject.name,REASON:.reason,MESSAGE:.message' 2>/dev/null \
    | grep -E "^LAST|${RELEASE}" | tail -n 10 >&2 || true
  exit 1
fi
read -r rev _ <<<"$(release_status)"
if [[ -z "${prev_rev:-}" ]]; then
  echo "  ${RELEASE}: installed, revision ${rev}"
elif diff <(helm -n "$NS" get manifest "$RELEASE" --revision "$prev_rev") \
          <(helm -n "$NS" get manifest "$RELEASE" --revision "$rev") >/dev/null; then
  echo "  ${RELEASE}: revision ${rev}, manifests identical to revision ${prev_rev}"
else
  echo "  ${RELEASE}: revision ${rev}, manifests changed since revision ${prev_rev}"
fi
pods="$(gateway_pods)"
after="$(awk '{ print $2 }' <<<"$pods")"
restarts="$(awk '{ s += $3 } END { print s + 0 }' <<<"$pods")"
names="$(awk '{ print $1 }' <<<"$pods" | tr '\n' ' ')"
if [[ -z "$before" ]]; then
  echo "  pods started: ${names% }"
elif [[ "$before" == "$after" ]]; then
  echo "  pods unchanged (${restarts} restarts): ${names% }"
else
  echo "  pods replaced: ${names% }"
fi
phase_done

# --- 3. The NLB -----------------------------------------------------------------
phase "network load balancer"
# shellcheck disable=SC2016
envsubst '${MY_IP}' <manifests/gateway-nlb.yaml >"$RENDERED/gateway-nlb.yaml"
want_ranges="[\"${MY_IP}/32\"]"
old_ranges="$(kubectl -n "$NS" get svc "$NLB_SVC" -o jsonpath='{.spec.loadBalancerSourceRanges}' 2>/dev/null || true)"
old_host="$(kubectl -n "$NS" get svc "$NLB_SVC" -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)"
applied_at=$(date +%s)
kubectl apply -f "$RENDERED/gateway-nlb.yaml" | sed 's/^/  /'
ranges_changed=""
if [[ -n "$old_ranges" && "$old_ranges" != "$want_ranges" ]]; then
  ranges_changed=1
  echo "  loadBalancerSourceRanges: ${old_ranges} -> ${want_ranges}"
fi

host=""
lb_arn=""
nlb_diagnose() {  # what the Load Balancer Controller said about the Service
  echo "--- events for service/${NLB_SVC}" >&2
  kubectl -n "$NS" get events --field-selector "involvedObject.name=${NLB_SVC}" \
    -o custom-columns='LAST SEEN:.lastTimestamp,REASON:.reason,MESSAGE:.message' 2>/dev/null | tail -n 8 >&2 || true
}
has_host() {
  host="$(kubectl -n "$NS" get svc "$NLB_SVC" -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)"
  [[ -n "$host" ]]
}
lb_state=""
is_active() {
  read -r lb_arn lb_state <<<"$(aws elbv2 describe-load-balancers --region "$AWS_REGION" \
    --query "LoadBalancers[?DNSName=='${host}'] | [0].[LoadBalancerArn, State.Code]" --output text 2>/dev/null || true)"
  [[ "${lb_state:-}" == active ]]
}
# The NLB's own security group carries loadBalancerSourceRanges (the controller's other group,
# shared by all its load balancers, has no address rules). Expect exactly one rule.
sg_rules=""
sg_ok() {
  local sgs
  sgs="$(aws elbv2 describe-load-balancers --region "$AWS_REGION" --load-balancer-arns "$lb_arn" \
    --query 'LoadBalancers[0].SecurityGroups' --output text 2>/dev/null || true)"
  [[ -n "$sgs" && "$sgs" != None ]] || { sg_rules="(no security groups)"; return 1; }
  # shellcheck disable=SC2086  # one argument per group ID
  sg_rules="$(aws ec2 describe-security-groups --region "$AWS_REGION" --group-ids $sgs --output json 2>/dev/null \
    | jq -r '[.SecurityGroups[].IpPermissions[] | . as $p | ((.IpRanges[]? | .CidrIp), (.Ipv6Ranges[]? | .CidrIpv6))
             | "\($p.IpProtocol)/\($p.FromPort) from \(.)"] | sort | join(", ")' 2>/dev/null || true)"
  [[ "$sg_rules" == "tcp/80 from ${MY_IP}/32" ]]
}
# Healthy means every live gateway pod is a healthy target (old pods may still be draining).
tg_arn=""
targets=""
targets_healthy() {
  local pod_ips health
  tg_arn="$(kubectl -n "$NS" get targetgroupbindings -o json 2>/dev/null \
    | jq -r --arg s "$NLB_SVC" '[.items[] | select(.spec.serviceRef.name == $s) | .spec.targetGroupARN][0] // empty' || true)"
  [[ -n "$tg_arn" ]] || return 1
  pod_ips="$(gateway_pods | awk '$4 == "true" { print $5 }' | jq -R . | jq -sc .)"
  health="$(aws elbv2 describe-target-health --region "$AWS_REGION" --target-group-arn "$tg_arn" --output json 2>/dev/null || true)"
  [[ -n "$health" ]] || health='{}'
  targets="$(jq -r --argjson ips "$pod_ips" '[.TargetHealthDescriptions[] | select(.Target.Id as $i | $ips | index($i))
    | "\(.Target.Id):\(.Target.Port) \(.TargetHealth.State)"] | sort | join(", ")' <<<"$health" 2>/dev/null || true)"
  jq -e --argjson ips "$pod_ips" '($ips | length) > 0 and ([.TargetHealthDescriptions[]
    | select(.Target.Id as $i | $ips | index($i)) | select(.TargetHealth.State == "healthy")] | length) == ($ips | length)' \
    <<<"$health" >/dev/null 2>&1
}
# The first time this machine resolves the DNS name and gets an answer from the gateway.
dns_at=""
answer=""
answers() {
  local rc=0
  answer="$(curl -s -m 5 -o /dev/null -w '%{http_code} %{time_total}' "http://${host}/status.ok" 2>/dev/null)" || rc=$?
  if [[ -z "$dns_at" && "$rc" != 6 ]]; then dns_at=$(date +%s); fi   # 6: could not resolve host
  [[ "$rc" == 0 && "${answer%% *}" == 200 ]]
}
blocked() {  # no TCP connection within 5 s: the security group drops it
  local rc=0
  curl -s -m 5 -o /dev/null "http://${host}/status.ok" 2>/dev/null || rc=$?
  [[ "$rc" == 28 ]]
}

# Five milestones, polled together every 5 s. Each is printed when it is first met, with the
# time since the apply above. On a re-run, what this run did not change is usually met at the
# first look ("already"). A new address is always timed: the security group and the answer from
# here depend on it, and the controller often updates the group within that first look.
# The address is only tried from here once the NLB is active and its security group lets this
# address in, so an early lookup can't leave a cached "no such name" on this machine.
at_name="" at_active="" at_sg="" at_health="" at_answer=""
first_look=1
met() {  # met LABEL TIMINGS-LABEL [CHANGED]: CHANGED non-empty when this run changed it
  local t
  if (( first_look )) && [[ -n "$old_host" && -z "${3:-}" ]]; then
    printf '  %-50s already\n' "$1"
  else
    t="$(since "$applied_at")"
    printf '  %-50s after %s\n' "$1" "$t"
    timings+="$(printf '  %-30s %9s' "  NLB: $2" "$t")"$'\n'
  fi
}
milestones() {  # one look at each milestone not met yet; true when all are met
  if [[ -z "$at_name" ]] && has_host; then
    at_name=$(date +%s)
    met "DNS name in the Service status" "DNS name"
    echo "    ${host}"
  fi
  [[ -n "$at_name" ]] || return 1
  if [[ -z "$at_active" ]] && is_active; then at_active=$(date +%s); met "NLB active" "active"; fi
  if [[ -z "$at_sg" && -n "$lb_arn" && "$lb_arn" != None ]] && sg_ok; then
    at_sg=$(date +%s)
    met "security group: tcp/80 from ${MY_IP}/32 only" "security group" "$ranges_changed"
  fi
  if [[ -z "$at_health" ]] && targets_healthy; then at_health=$(date +%s); met "gateway pods healthy in the target group" "targets healthy"; fi
  if (( expect_reachable )) && [[ -z "$at_answer" && -n "$at_active" && -n "$at_sg" ]] && answers; then
    at_answer=$(date +%s)
    met "gateway answers from here" "answers from here" "$ranges_changed"
    if [[ -z "$old_host" && -n "$dns_at" ]] && (( dns_at < at_answer )); then
      echo "    (the name first resolved here after $(duration $(( dns_at - applied_at ))))"
    fi
  fi
  [[ -n "$at_active" && -n "$at_sg" && -n "$at_health" ]] || return 1
  (( ! expect_reachable )) || [[ -n "$at_answer" ]]
}
all_met=0
if milestones; then all_met=1; fi
first_look=0
if (( ! all_met )) && ! wait_until "$NLB_TIMEOUT" milestones; then
  echo "the NLB is not ready after ${NLB_TIMEOUT}s:" >&2
  [[ -n "$at_name" ]] || echo "  no DNS name in the status of service/${NLB_SVC}" >&2
  [[ -n "$at_active" || -z "$at_name" ]] || echo "  NLB state: ${lb_state:-not found}" >&2
  [[ -n "$at_sg" || -z "$at_name" ]] || echo "  NLB security group: ${sg_rules:-no rules}, expected tcp/80 from ${MY_IP}/32 only" >&2
  [[ -n "$at_health" || -z "$at_name" ]] || echo "  targets: ${targets:-none registered}" >&2
  if (( expect_reachable )) && [[ -n "$at_active" && -n "$at_sg" && -z "$at_answer" ]]; then
    echo "  http://${host}/status.ok from here: ${answer:-no answer}" >&2
  fi
  nlb_diagnose
  exit 1
fi
phase_done

# --- 4. The server announces the gateway ---------------------------------------
phase "server"
# The values 04_install.sh adds to the server release. Rewritten only when they change, so
# the file's content says what the server should announce.
desired="# Written by 05_gateway.sh, read by 04_install.sh. Gitignored: it holds this cluster's NLB
# DNS name. Run ./05_gateway.sh again instead of editing it.
server:
  gateway:
    enabled: true
    host: ${host}
    gatewayRouteMode: ${ROUTE_MODE}"
if [[ -f "$GATEWAY_VALUES" && "$(cat "$GATEWAY_VALUES")" == "$desired" ]]; then
  echo "  ${GATEWAY_VALUES} unchanged"
else
  printf '%s\n' "$desired" >"$RENDERED/server-gateway.yaml"
  mv "$RENDERED/server-gateway.yaml" "$GATEWAY_VALUES"
  echo "  ${GATEWAY_VALUES} written: gateway.enabled true, host ${host}, route mode ${ROUTE_MODE}"
fi
want_values="$(jq -cnS --arg h "$host" --arg m "$ROUTE_MODE" '{enabled: true, host: $h, gatewayRouteMode: $m}')"
live_values="$(helm -n "$NS" get values opensandbox-server -o json | jq -cS '.server.gateway // {}')"
if [[ "$live_values" == "$want_values" ]]; then
  echo "  the server release already announces the gateway: ./04_install.sh not needed"
else
  echo "  upgrading the server with ./04_install.sh (full output in ${LOGS}/04_install.log)"
  if ! ./04_install.sh >"$LOGS/04_install.log" 2>&1; then
    tail -n 25 "$LOGS/04_install.log" >&2
    echo "./04_install.sh failed (full output in ${LOGS}/04_install.log)" >&2
    exit 1
  fi
  # What 04_install.sh did to the server, and its own checks if any failed.
  sed -n -E '/(server-gateway|opensandbox-server: (installed|revision)|pod opensandbox-server|FAIL|WARN)/p' "$LOGS/04_install.log" \
    | sed -E 's/^ */  04_install.sh: /'
fi
phase_done

# --- 5. Checks ------------------------------------------------------------------
phase "checks"
s="$(release_status)"
if [[ "${s#* }" == deployed ]]; then ok "release ${RELEASE} deployed (revision ${s%% *})"; else fail "release ${RELEASE}: ${s:-not installed}"; fi
n_ready=0
while read -r name _ restarts ready ip node; do
  [[ -n "$name" ]] || continue
  zone="$(kubectl get node "$node" -o jsonpath='{.metadata.labels.topology\.kubernetes\.io/zone}' 2>/dev/null || true)"
  if [[ "$ready" == true ]]; then
    ok "${name} ready, ${restarts} restarts, ${ip} on ${node} (${zone:-?})"
    n_ready=$((n_ready + 1))
  else
    fail "${name} not ready, ${restarts} restarts"
  fi
done <<<"$(gateway_pods)"
(( n_ready > 0 )) || fail "no gateway pod is ready"
image="$(kubectl -n "$NS" get deploy "$RELEASE" -o jsonpath='{.spec.template.spec.containers[0].image}')"
if [[ "$image" == "${ECR_REGISTRY}/${ECR_PREFIX}/"* ]]; then ok "image from ECR: ${image#"${ECR_REGISTRY}/"}"; else fail "image not from ECR: ${image}"; fi

ranges="$(kubectl -n "$NS" get svc "$NLB_SVC" -o jsonpath='{.spec.loadBalancerSourceRanges}')"
if [[ "$ranges" == "$want_ranges" ]]; then ok "service/${NLB_SVC} loadBalancerSourceRanges ${ranges}"; else fail "service/${NLB_SVC} loadBalancerSourceRanges ${ranges}, expected ${want_ranges}"; fi
lb="$(aws elbv2 describe-load-balancers --region "$AWS_REGION" --load-balancer-arns "$lb_arn" --output json 2>/dev/null \
  | jq -r '.LoadBalancers[0] | "\(.Scheme) \(.Type) \(.State.Code) \([.AvailabilityZones[].ZoneName] | join(","))"' 2>/dev/null || true)"
listeners="$(aws elbv2 describe-listeners --region "$AWS_REGION" --load-balancer-arn "$lb_arn" --output json 2>/dev/null \
  | jq -r '[.Listeners[] | "\(.Protocol):\(.Port)"] | join(" ")' 2>/dev/null || true)"
if [[ "$lb" == "internet-facing network active "* && "$listeners" == "TCP:80" ]]; then
  ok "NLB internet-facing, active, listener ${listeners}, zones ${lb##* }"
else
  fail "NLB: ${lb:-not found}, listeners ${listeners:-none} (expected internet-facing network active, TCP:80)"
fi
if sg_ok; then ok "NLB security group: ${sg_rules} (no other rules)"; else fail "NLB security group: ${sg_rules:-no rules}, expected tcp/80 from ${MY_IP}/32 only"; fi
if targets_healthy; then ok "NLB targets: ${targets}"; else fail "NLB targets: ${targets:-none}"; fi

if (( expect_reachable )); then
  if answers; then ok "http://${host}/status.ok from here: ${answer%% *} in $(awk '{ printf "%d ms", $2 * 1000 }' <<<"$answer")"; else fail "http://${host}/status.ok from here: ${answer:-no answer}"; fi
  # Header routing is live: a request for a sandbox that does not exist gets the gateway's own 404.
  route="$(curl -s -m 5 -w '\n%{http_code}' -H "OpenSandbox-Ingress-To: no-such-sandbox-${EXECD_PORT}" "http://${host}/ping" 2>/dev/null || true)"
  route_code="$(sed -n '$p' <<<"$route")"
  route_body="$(sed '$d' <<<"$route" | tr -s '\n' ' ')"
  if [[ "$route_code" == 404 ]]; then
    ok "OpenSandbox-Ingress-To: no-such-sandbox-${EXECD_PORT} -> 404 ${route_body% }"
  else
    fail "OpenSandbox-Ingress-To for a missing sandbox: ${route_code:-no answer} ${route_body% }, expected 404"
  fi
else
  # The security group no longer lists this machine, so its connection gets no answer at all.
  if blocked; then
    ok "this machine (${detected}) gets no answer within 5 s: only ${MY_IP}/32 gets through"
  elif answers; then
    fail "this machine (${detected}) still reaches the gateway although only ${MY_IP}/32 is allowed"
  else
    warn "this machine (${detected}): no answer, but not a timeout either (${answer:-no response})"
  fi
fi

toml="$(kubectl -n "$NS" get configmap opensandbox-server-config -o jsonpath='{.data.config\.toml}')"
ingress="$(awk '$0 == "[ingress]" { s = 1; next } /^\[/ { s = 0 } s && NF' <<<"$toml" | tr -d '"' | tr '\n' ' ' | sed 's/ = /=/g')"
if [[ "$ingress" == "mode=gateway gateway.address=${host} gateway.route.mode=${ROUTE_MODE} " ]]; then
  ok "server config.toml [ingress]: ${ingress% }"
else
  fail "server config.toml [ingress]: ${ingress:-missing}, expected mode=gateway gateway.address=${host} gateway.route.mode=${ROUTE_MODE}"
fi
server_pod="$(kubectl -n "$NS" get pods -l app.kubernetes.io/name=opensandbox-server -o json \
  | jq -r '[.items[] | select(.metadata.deletionTimestamp == null and all(.status.containerStatuses[]?; .ready))] | length')"
if [[ "$server_pod" == 1 ]]; then ok "server pod ready"; else fail "${server_pod} ready server pods, expected 1"; fi
phase_done

# --- Summary --------------------------------------------------------------------
printf '\n\033[1m== Timings\033[0m\n%s' "$timings"
printf '  %-30s %9s\n' "total" "$(duration $(( $(date +%s) - script_start )))"
if (( failures > 0 )); then
  echo "${failures} check(s) failed" >&2
  exit 1
fi
echo "Sandbox endpoints now point at http://${host} (header OpenSandbox-Ingress-To), open to ${MY_IP}/32."
if (( ! expect_reachable )); then echo "This machine is shut out: run ./05_gateway.sh without MY_IP to let it back in."; fi
