#!/usr/bin/env bash
# On the instance, as the agent user: look at Enclave, then the first run, which builds the images.
# Memory is sampled every 5 s while it builds.
set -uo pipefail
# Enclave refuses to mount a home directory, so the agent works in a project directory
mkdir -p ~/project && cd ~/project
[ -d .git ] || { git init -q . && echo "# demo project" > README.md && git add . && git commit -qm init; }
mkdir -p ~/.config/enclave && echo '{"tool":"claude"}' > ~/.config/enclave/config.json

echo "== enclave info (before the first run)"; enclave info 2>&1 | grep -E '^(Project Directory|Tool|Image Name|Image Present)'
echo "== tools"; enclave tools list 2>&1 | tr '\n' ' '; echo

echo "== first run: claude --version inside a session"
( while sleep 5; do echo "$(date +%s) $(free -m | awk '/Mem/{print $3}') $(free -m | awk '/Swap/{print $3}')"; done > /tmp/mem.log ) &
SAMPLER=$!
START=$(date +%s)
# Enclave wants a terminal, and SSM Run Command has none: `script` provides one.
script -qec "enclave --tool claude --progress compact -- --version" /dev/null 2>&1 \
  | sed 's/\x1b\[[0-9;?]*[a-zA-Z]//g' | tr -d '\r' | grep -E '^(ok|info|warn|error):|Claude Code' | grep -v 'auth store\|mounted\|Creating'
echo "first run took $(( $(date +%s) - START )) s"
kill $SAMPLER 2>/dev/null
echo "peak RAM used: $(sort -k2 -n /tmp/mem.log | tail -1 | awk '{print $2}') MB of $(free -m | awk '/Mem/{print $2}') MB, peak swap used: $(sort -k3 -n /tmp/mem.log | tail -1 | awk '{print $3}') MB"
echo "== images"; docker images --format '{{.Repository}}:{{.Tag}}  {{.Size}}'

echo "== second run, images already built"
START=$(date +%s)
script -qec "enclave --tool claude -- --version" /dev/null 2>&1 | sed 's/\x1b\[[0-9;?]*[a-zA-Z]//g' | tr -d '\r' | grep 'Claude Code'
echo "second run took $(( $(date +%s) - START )) s"
