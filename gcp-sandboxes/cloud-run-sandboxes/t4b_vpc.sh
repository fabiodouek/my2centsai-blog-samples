#!/usr/bin/env bash
# Test 4b (optional): does --allow-egress follow the service's VPC egress setting?
# A second service, sbx-internal, accepts only traffic from inside the VPC (ingress: internal) and needs a
# token. We send it NO token: "403" means the request arrived through the VPC and was refused for lack of a
# token; "404" means it came in from outside and was never let in. Usage: ./t4b_vpc.sh   (from your laptop)
cd "$(dirname "$0")"
source ./lib.sh
IU=$(gcloud run services describe sbx-internal --format='value(status.url)')
cat > /tmp/t4b_remote.sh <<EOS
SB=/usr/local/gcp/bin/sandbox
row() { printf '%-44s %s\n' "\$1" "\$2"; }
row "host curl (no token)"                  "HTTP \$(curl -m5 -s -o /dev/null -w '%{http_code}' $IU/)"
row "sandbox, default (no egress)"          "\$(\$SB do -- /usr/bin/curl -m5 -sS -o /dev/null -w 'HTTP %{http_code}' $IU/ 2>&1 | head -1)"
row "sandbox --allow-egress"                "\$(\$SB do --allow-egress -- /usr/bin/curl -m5 -sS -o /dev/null -w 'HTTP %{http_code}' $IU/ 2>&1 | head -1)"
EOS
echo "== VPC egress setting of the service: $(gcloud run services describe "$SERVICE" --format='value(spec.template.metadata.annotations."run.googleapis.com/vpc-access-egress")')"
run_on_instance /tmp/t4b_remote.sh 60 | grep -v '^\[exit' | sed -E 's/sbx-internal-[a-z0-9.-]+/sbx-internal/'
