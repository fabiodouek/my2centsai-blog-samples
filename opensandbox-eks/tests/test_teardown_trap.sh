#!/usr/bin/env bash
# Checks 99_teardown.sh's ERR trap with a stub `aws`: no AWS account, cluster or network needed.
#
# The trap prints "teardown stopped at line N" when a command fails. Under `set -E` bash also runs it inside
# $(...), and some of those failures are the expected answer: the checks that wait for the NLB, the volume and
# the cluster to be gone get "not found" from AWS. The trap therefore only prints in the main shell. This test
# takes the script's own `trap '...' ERR` lines (the shipped code, not a copy), runs them under
# `set -Eeuo pipefail` with an `aws` that always fails, and checks:
#   A, B  the "is it gone yet?" checks (nlb_gone and vol_gone in a wait loop; `if out="$(aws eks describe-cluster)"`)
#         print no stop message, and the script goes on
#   C     a real failure in the main shell prints the message once, with the right line number, and stops
#   D     a failed assignment from $(...) prints it once and stops
#   E     a failed pipeline (pipefail) prints it once and stops
#   F     a failure inside a function prints it once and stops
#
# usage: bash tests/test_teardown_trap.sh [path/to/99_teardown.sh] [bash binary]
#        (defaults: ../99_teardown.sh next to this directory, and the first bash on PATH, as the script's
#        shebang uses; exit 0 when every case passes)
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="${1:-$here/../99_teardown.sh}"
BASH_BIN="${2:-$(command -v bash)}"
[[ -f "$SCRIPT" ]] || { echo "no such script: $SCRIPT" >&2; exit 2; }
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# bash 3.2 (macOS) has no mapfile
traps=()
while IFS= read -r line; do traps+=("$line"); done < <(grep -E "^trap '.*' ERR$" "$SCRIPT")
pass=0
failed=0
check() {  # check <description> <exit status of the condition, run just before>
  if (( $2 == 0 )); then printf '  PASS  %s\n' "$1"; pass=$((pass + 1)); else printf '  FAIL  %s\n' "$1"; failed=$((failed + 1)); fi
}

(( ${#traps[@]} == 2 )) && [[ "${traps[0]}" == "${traps[1]}" ]]
check "the script has two ERR traps (the first, and the one set again after terraform destroy), and they are identical" $?
TRAP="${traps[0]:-}"

mkdir -p "$tmp/bin"
cat >"$tmp/bin/aws" <<'EOF'
#!/bin/sh
# stub: every call fails the way the AWS CLI does for a resource that is gone
case "$*" in
  *describe-cluster*) echo "An error occurred (ResourceNotFoundException) when calling the DescribeCluster operation" >&2 ;;
  *volume*)           echo "An error occurred (InvalidVolume.NotFound) when calling the DescribeVolumes operation" >&2 ;;
  *)                  echo "An error occurred (LoadBalancerNotFound) when calling the DescribeLoadBalancers operation" >&2 ;;
esac
exit 254
EOF
chmod +x "$tmp/bin/aws"

out="" err="" rc=0 msgs=0
run_case() {  # run_case <name> <body>: runs the trap and the body under set -Eeuo pipefail; sets out, err, rc, msgs
  local name="$1" body="$2"
  printf '%s\n' "set -Eeuo pipefail" "$TRAP" "$body" >"$tmp/$name.sh"
  out="$(PATH="$tmp/bin:$PATH" "$BASH_BIN" "$tmp/$name.sh" 2>"$tmp/$name.err")"
  rc=$?
  err="$(cat "$tmp/$name.err")"
  msgs=$(grep -c 'teardown stopped at line' "$tmp/$name.err" || true)
}

# shellcheck disable=SC2016  # the case bodies are bash for the child shell, expanded there
run_case A 'wait_for() { local limit="$1" waited=0; shift; until "$@"; do (( waited >= limit )) && return 1; waited=$((waited + 1)); done; }
nlb_gone() { local e; e="$(aws elbv2 describe-load-balancers --load-balancer-arns arn 2>&1 >/dev/null)" && return 1; [[ "$e" == *LoadBalancerNotFound* ]]; }
vol_gone() { local e; e="$(aws ec2 describe-volumes --volume-ids vol 2>&1 >/dev/null)" && return 1; [[ "$e" == *InvalidVolume.NotFound* ]]; }
if wait_for 3 nlb_gone; then echo "ok the NLB is gone"; fi
if wait_for 3 vol_gone; then echo "ok the volume is gone"; fi
echo "A went on"'
(( msgs == 0 )) && (( rc == 0 )) && [[ "$out" == *"ok the NLB is gone"*"ok the volume is gone"*"A went on"* ]]
check "A: the NLB and volume checks print no stop message and the script goes on" $?

# shellcheck disable=SC2016
run_case B 'if describe_out="$(aws eks describe-cluster --name osb-eks 2>&1)"; then echo "still exists"
elif [[ "$describe_out" == *ResourceNotFoundException* ]]; then echo "ok the cluster is gone"; fi
echo "B went on"'
(( msgs == 0 )) && (( rc == 0 )) && [[ "$out" == *"ok the cluster is gone"*"B went on"* ]]
check "B: the cluster check prints no stop message and the script goes on" $?

run_case C 'echo "before"
aws ecr delete-repository --repository-name x
echo "after (must not run)"'
(( msgs == 1 )) && (( rc != 0 )) && [[ "$err" == *"teardown stopped at line 4 "* ]] && [[ "$out" != *"after"* ]]
check "C: a real failure in the main shell prints the message once, at line 4, and stops" $?

# shellcheck disable=SC2016
run_case D 'caller="$(aws sts get-caller-identity --query Account --output text)"
echo "after (must not run)"'
(( msgs == 1 )) && (( rc != 0 )) && [[ "$out" != *"after"* ]]
check "D: a failed assignment from \$(...) prints the message once and stops" $?

run_case E 'aws ecr describe-repositories | sed "s/^/  /"
echo "after (must not run)"'
(( msgs == 1 )) && (( rc != 0 )) && [[ "$out" != *"after"* ]]
check "E: a failed pipeline prints the message once and stops" $?

run_case F 'step() { aws eks describe-nodegroup --cluster-name osb-eks; }
step
echo "after (must not run)"'
(( msgs == 1 )) && (( rc != 0 )) && [[ "$out" != *"after"* ]]
check "F: a failure inside a function prints the message once and stops" $?

printf '%d passed, %d failed (%s)\n' "$pass" "$failed" "$("$BASH_BIN" --version | head -n 1)"
(( failed == 0 ))
