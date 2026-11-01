#!/usr/bin/env bash
# Test 1: a sandbox's life. The main process (the command after --) under the three restart
# policies, the generation that changes on every restart, stop and start, suspend, and a
# sandbox that deletes itself when its command exits (--no-keep).
source "$(dirname "$0")/lib.sh"
cleanup() { for n in rp-never rp-onfail rp-always life oneshot; do os sandbox delete "$n" >/dev/null 2>&1 & done; wait; }
trap cleanup EXIT
cleanup

say "openshell sandbox create --name rp-never --restart-policy never -- sh -c 'sleep 5; exit 3'"
os sandbox create --name rp-never --restart-policy never --detach --no-tty -- sh -c 'sleep 5; exit 3' >/dev/null 2>&1
watch_sandbox rp-never 45 '^(Completed|Error)$'

say "openshell sandbox create --name rp-onfail --restart-policy on-failure -- sh -c 'sleep 5; exit 3'"
os sandbox create --name rp-onfail --restart-policy on-failure --detach --no-tty -- sh -c 'sleep 5; exit 3' >/dev/null 2>&1
watch_sandbox rp-onfail 75

say "openshell sandbox create --name rp-always --restart-policy always -- sh -c 'sleep 5; exit 0'"
os sandbox create --name rp-always --restart-policy always --detach --no-tty -- sh -c 'sleep 5; exit 0' >/dev/null 2>&1
watch_sandbox rp-always 45

say "openshell sandbox create --name life   (then stop, start, suspend)"
os sandbox create --name life --detach --no-tty >/dev/null 2>&1
echo "  ready, generation $(sandbox_state life | awk '{print $5}')"
t=$(now); os sandbox stop life >/dev/null 2>&1; echo "  stop:  $(sandbox_state life | awk '{print $1}') after $(since "$t") s"
kubectl -n "$NS" get pods -l "openshell.ai/sandbox-id=$(os sandbox get life -o json | jq -r .id)" --no-headers 2>/dev/null | wc -l | xargs echo "  supervisor pods while stopped:"
t=$(now); os sandbox start life >/dev/null 2>&1
watch_sandbox life 90 '^Ready$' >/dev/null
echo "  start: $(sandbox_state life | awk '{print $1}') after $(since "$t") s, generation $(sandbox_state life | awk '{print $5}')"
say "openshell sandbox suspend life"
os sandbox suspend life 2>&1 | sed 's/\x1b\[[0-9;]*m//g' | grep -E 'message|×' | head -n 2

say "openshell sandbox create --name oneshot --no-keep -- echo 'hello from a one-shot sandbox'"
t=$(now)
os sandbox create --name oneshot --no-keep --no-tty -- echo "hello from a one-shot sandbox" 2>&1 | sed 's/\x1b\[[0-9;]*m//g' | grep -E 'hello' | head -n 1
for _ in $(seq 1 60); do os sandbox get oneshot >/dev/null 2>&1 || break; sleep 1; done
echo "  oneshot gone after $(since "$t") s: $(os sandbox get oneshot >/dev/null 2>&1 && echo 'still listed' || echo 'not listed')"
