#!/usr/bin/env bash
# ./run.sh <script> [timeout]  — run one of the on-instance scripts (t*.sh) via the command endpoint
set -euo pipefail
cd "$(dirname "$0")"
source ./lib.sh
run_on_instance "$1" "${2:-900}"
