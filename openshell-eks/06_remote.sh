#!/usr/bin/env bash
# Use the gateway from outside the cluster, through the NLB 05_install.sh created, instead of
# a port-forward. The NLB passes TCP through, so the CLI's mTLS client certificate reaches the
# gateway itself; the NLB only admits one source address.
#
#   ./06_remote.sh            allow your current public IP (or MY_IP), then status and list
#   ./06_remote.sh --delete   delete the NLB Service and the osh-eks-nlb CLI registration
#
# Safe to run again; your IP is looked up every time, so a new IP only needs a re-run.
set -euo pipefail
cd "$(dirname "$0")"

NS="openshell"
SERVICE="openshell-gateway-nlb"
REMOTE="osh-eks-nlb"   # CLI registration for the NLB endpoint
LOCAL="osh-eks"        # the port-forward registration from 05_install.sh
CFG="${XDG_CONFIG_HOME:-$HOME/.config}/openshell/gateways"

[[ -f env.sh ]] || { echo "env.sh not found: run ./01_cluster.sh first" >&2; exit 1; }
# shellcheck source=/dev/null
source ./env.sh
kubectl() { command kubectl --context "$KUBE_CONTEXT" "$@"; }

if [[ "${1:-}" == "--delete" ]]; then
  kubectl -n "$NS" delete service "$SERVICE" --ignore-not-found --wait=true >/dev/null
  openshell gateway remove "$REMOTE" >/dev/null 2>&1 || true
  echo "deleted ${SERVICE} and the ${REMOTE} registration"
  # The AWS Load Balancer Controller deletes the NLB once the Service is gone.
  for _ in $(seq 1 30); do
    left="$(aws elbv2 describe-load-balancers --region "$AWS_REGION" --query "LoadBalancers[?starts_with(LoadBalancerName, 'k8s-openshel')].LoadBalancerName" --output text)"
    [[ -z "$left" ]] && { echo "no k8s-openshel* load balancer left"; exit 0; }
    sleep 5
  done
  echo "load balancer still there after 150 s: ${left}" >&2
  exit 1
fi

host="$(kubectl -n "$NS" get service "$SERVICE" -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')"
[[ -n "$host" ]] || { echo "${SERVICE} has no hostname: run ./05_install.sh" >&2; exit 1; }

MY_IP="${MY_IP:-$(curl -fsS -m 10 https://checkip.amazonaws.com)}"
kubectl -n "$NS" patch service "$SERVICE" --type merge -p "{\"spec\":{\"loadBalancerSourceRanges\":[\"${MY_IP}/32\"]}}" >/dev/null
echo "source range: $(kubectl -n "$NS" get service "$SERVICE" -o jsonpath='{.spec.loadBalancerSourceRanges}')"

# The AWS Load Balancer Controller copies the range into the NLB's frontend security group a
# few seconds later; wait for that, so the test below runs against the new rule.
lb_sgs() {
  aws elbv2 describe-load-balancers --region "$AWS_REGION" \
    --query "LoadBalancers[?DNSName=='${host}'].SecurityGroups[]" --output text
}
for _ in $(seq 1 40); do
  cidrs="$(aws ec2 describe-security-groups --region "$AWS_REGION" --group-ids $(lb_sgs) \
    --query 'SecurityGroups[].IpPermissions[?ToPort==`8080`][].IpRanges[].CidrIp' --output text 2>/dev/null || true)"
  [[ "$cidrs" == "${MY_IP}/32" ]] && break
  sleep 3
done
echo "NLB security group allows 8080 from: ${cidrs:-nothing}"

# Register, then copy in the same client certificate as the port-forward registration
# (`gateway add --local` would leave a local gateway's certificates there).
mkdir -p "${CFG}/${REMOTE}/mtls"
if ! openshell gateway list 2>/dev/null | grep -qw "$REMOTE"; then
  openshell gateway add "https://${host}:8080" --local --name "$REMOTE" >/dev/null 2>&1 || true
fi
cp "${CFG}/${LOCAL}/mtls/"{ca.crt,tls.crt,tls.key} "${CFG}/${REMOTE}/mtls/"
chmod 600 "${CFG}/${REMOTE}/mtls/tls.key"
# gateway add makes the new registration active; the port-forward one stays the default.
openshell gateway select "$LOCAL" >/dev/null

echo "certificate SANs: $(openssl s_client -connect "${host}:8080" -servername "$host" </dev/null 2>/dev/null \
  | openssl x509 -noout -ext subjectAltName 2>/dev/null | tail -n 1 | tr -d ' ' || echo unreachable)"

# A changed source range takes a little while to reach the NLB's security group.
for i in $(seq 1 24); do
  if openshell status -g "$REMOTE" >/dev/null 2>&1; then break; fi
  if (( i == 24 )); then
    echo "gateway not reachable through ${host} from ${MY_IP} after 2 minutes" >&2
    exit 1
  fi
  sleep 5
done
openshell status -g "$REMOTE"
openshell sandbox list -g "$REMOTE"
code="$(curl -s -m 5 -o /dev/null -w '%{http_code}' "http://${host}:9090/metrics" || true)"
echo "metrics port 9090 through the NLB: ${code:-no answer} (not exposed)"
