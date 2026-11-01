#!/usr/bin/env bash
# Test 9: the policy prover, the way you'd run it in CI. openshell-prover checks that a candidate
# policy allows nothing its boundary doesn't (a Z3 query over binaries, hosts, ports, IPs, and REST
# methods and paths). Exit codes: 0 within, 1 exceeds, 2 error, 3 unsupported or inconclusive.
# It runs on the laptop; no cluster needed.
set -uo pipefail
cd "$(dirname "$0")/prover"
openshell-prover --version
for c in candidate-ok candidate-wildcard candidate-post candidate-audit; do
  printf '\n\033[1m$ openshell-prover check --boundary boundary.yaml %s.yaml\033[0m\n' "$c"
  openshell-prover check --boundary boundary.yaml "$c.yaml" | grep -vE '^coverage:' | sed 's/^/  /'
  echo "  exit code: ${PIPESTATUS[0]}"
done
