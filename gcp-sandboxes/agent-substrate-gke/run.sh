#!/usr/bin/env bash
# ./run.sh t3a_create.py : run one script with the right atespace and keep its output in results/<script>.txt.
# Lifecycle-style tests (t2a, t3, t4, t5, t10b) use 'lifecycle', the stress-style ones use 'stress'
# (the same split as the EKS scripts). Starts the steady port-forward to ax-server if it is not running.
set -euo pipefail
cd "$(dirname "$0")"; source ./env.sh
pgrep -f ax_forward.sh >/dev/null || { (nohup ./ax_forward.sh >/dev/null 2>&1 &); sleep 3; }
case "$1" in t2a*|t3*|t4*|t5*|t10b*) export ATESPACE=lifecycle;; *) export ATESPACE=stress;; esac
mkdir -p results
{ echo "# $(date -u +%FT%TZ) $*"; python3 -u "$@"; } 2>&1 | tee "results/${1%.py}.txt"
