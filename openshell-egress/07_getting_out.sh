#!/usr/bin/env bash
# Test 7: ways out of a sandbox on EKS, and which layer stops each: instance metadata and the EKS
# Pod Identity agent (link-local), the kubelet, the Kubernetes API, direct IPs, other ports on the
# node, and UDP. The node IP is masked as <node-ip>.
source "$(dirname "$0")/lib.sh"
NAME="out"
cleanup() { os sandbox delete "$NAME" >/dev/null 2>&1; }
trap cleanup EXIT
say "openshell sandbox create --name ${NAME} --policy policies/echo-read-only.yaml"
make_sandbox "$NAME" --policy "${HERE}/policies/echo-read-only.yaml" || exit 1
NODE_IP="$(kubectl -n "$NS" get pod "default--${NAME}" -o jsonpath='{.status.hostIP}')"
mask() { sed "s/${NODE_IP//./\\.}/<node-ip>/g"; }
add() {  # endpoint -> one line: loaded, or the gateway's reason for refusing it
  os policy update "$NAME" --add-endpoint "$1" --binary /usr/bin/curl --wait 2>&1 | nocolor \
    | tr '\n' ' ' | sed -E 's/│//g; s/ +/ /g; s/.*(Policy version [0-9]+ loaded).*/\1/; s/.*message: "([^"]*)".*/refused: \1/; s/;.*//; s/ \(loopback\/ link-local\/unspecified\)//' | cut -c1-120
}
http() { in_sb "$NAME" sh -c "curl -sS -k -m 8 -o /dev/null -w '%{http_code}' $1 2>&1 | tail -n 1"; }
row() { printf '  %-44s %s\n' "$1" "$2"; }

say "policy update --add-endpoint for link-local addresses"
row "169.254.169.254:80 (instance metadata)" "$(add 169.254.169.254:80)"
row "169.254.170.23:80 (Pod Identity agent)" "$(add 169.254.170.23:80)"
row "allowed-ip=169.254.0.0/16" "$(add 'metadata.example:80::::allowed-ip=169.254.0.0/16')"

say "the kubelet on the node, allowed by policy"
row "add <node-ip>:10250" "$(add "${NODE_IP}:10250" | mask)"
row "curl https://<node-ip>:10250/pods" "$(http "https://${NODE_IP}:10250/pods")"
os logs "$NAME" -n 50 --level warn 2>/dev/null | nocolor | grep -o 'reason=port 10250[^,]*' | head -n 1 | sed 's/^/  /'

say "direct IPs and other ports"
row "curl https://1.1.1.1/ (not in policy)" "$(http https://1.1.1.1/)"
row "add 1.1.1.1:443" "$(add 1.1.1.1:443)"
row "curl https://1.1.1.1/" "$(http https://1.1.1.1/)"
row "python3 -> <node-ip>:22" "$(in_sb "$NAME" python3 -c "import socket; socket.create_connection(('${NODE_IP}', 22), timeout=3)" 2>&1 | tail -n 1)"

say "UDP"
in_sb "$NAME" python3 -c "
import socket
for dst in [('8.8.8.8', 53), ('${NODE_IP}', 40001)]:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try: s.sendto(b'x', dst); r = 'sent'
    except OSError as e: r = f'{type(e).__name__}: {e}'
    print(f'  sendto {dst[0]}:{dst[1]}'.ljust(46), r)" 2>&1 | mask

say "the Kubernetes API, allowed by policy"
row "add kubernetes.default.svc.cluster.local:443" "$(add kubernetes.default.svc.cluster.local:443)"
row "curl /version (TLS terminated)" "$(http https://kubernetes.default.svc.cluster.local/version)"
ocsf "$NAME" | grep -E 'kubernetes' | grep -E 'FAIL' | cut -c1-80 | sed 's/^/  /' | tail -n 1
os policy set "$NAME" --policy "${HERE}/policies/k8s-api-passthrough.yaml" --wait >/dev/null 2>&1
row "with tls: skip, curl /version" "$(http https://kubernetes.default.svc.cluster.local/version)"
row "curl /api/v1/namespaces/openshell/secrets" "$(http https://kubernetes.default.svc.cluster.local/api/v1/namespaces/openshell/secrets)"
