#!/usr/bin/env bash
# Test 8a: what do busy sandboxes do to the host's own requests? Run from your laptop.
# Sends N sequential requests to the service's "/" (a tiny handler) at idle, then with 4 and
# with 8 busy-loop sandboxes on the same 2 vCPU / 4 GiB instance.
set -euo pipefail
cd "$(dirname "$0")"
source ./lib.sh
N=${N:-100}
URL=$(svc_url); TOKEN=$(gcloud auth print-identity-token)

probe() {  # probe <label>: N timed requests from here, in milliseconds
  for i in $(seq "$N"); do
    curl -s -o /dev/null -m 30 -H "Authorization: Bearer $TOKEN" -w '%{time_total}\n' "$URL/"
  done | awk '{print $1 * 1000}' | sort -n | awk -v label="$1" '{a[NR]=$1; s+=$1}
    END {printf "%-22s n=%d  mean=%6.0f ms  p50=%6.0f  p95=%6.0f  max=%6.0f\n", label, NR, s/NR, a[int(NR*0.5)], a[int(NR*0.95)], a[NR]}'
}
hostwork() {  # hostwork <label>: a fixed CPU-bound job run by the host itself (about 0.3 s alone), 5 times
  local f; f=$(mktemp)
  cat > "$f" <<'EOW'
for i in 1 2 3 4 5; do
  a=$(date +%s%N); python3 -c 'sum(i*i for i in range(3_000_000))'; b=$(date +%s%N); echo $(( (b - a) / 1000000 ))
done | awk -v label="$LABEL" '{s+=$1; if ($1>m) m=$1} END {printf "%-22s host CPU job: mean=%5.0f ms  max=%5.0f\n", label, s/NR, m}'
EOW
  { echo "LABEL='$1'"; cat "$f"; } > "$f.2"; run_on_instance "$f.2" 120 | grep -v '^\[exit'; rm -f "$f" "$f.2"
}
on_instance() { local f; f=$(mktemp); cat > "$f"; run_on_instance "$f" 120 | grep -v "^\[exit"; rm -f "$f"; }

run_on_instance t8_unhog.sh 60 >/dev/null
probe "idle"; hostwork "idle"
{ echo "FIRST=1 LAST=4"; cat t8_hogs.sh; } | on_instance
probe "4 busy sandboxes"; hostwork "4 busy sandboxes"
{ echo "FIRST=5 LAST=8"; cat t8_hogs.sh; } | on_instance
probe "8 busy sandboxes"; hostwork "8 busy sandboxes"
run_on_instance t8_unhog.sh 60 | grep -v "^\[exit"
