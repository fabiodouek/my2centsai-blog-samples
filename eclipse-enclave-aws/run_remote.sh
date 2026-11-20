#!/usr/bin/env bash
# Run a script from remote/ on the instance through SSM Run Command and print its output.
# usage: ./run_remote.sh remote/01_install.sh [user] [script arguments]      (user defaults to root; use "agent" for Enclave)
. "$(dirname "$0")/common.sh"
[ -n "${INSTANCE_ID:-}" ] || { echo "no instance: run 03_wait_ssm.sh first" >&2; exit 1; }
ssm_run "$HERE/$1" "${2:-root}" "${3:-}"
