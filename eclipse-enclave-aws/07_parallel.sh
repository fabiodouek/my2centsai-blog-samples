#!/usr/bin/env bash
# Two agents at once on the 2 GiB host, then a root-level check of the kernel log for out-of-memory kills.
. "$(dirname "$0")/common.sh"
"$HERE/run_remote.sh" remote/07_parallel.sh agent
"$HERE/run_remote.sh" remote/07b_oom_check.sh
