#!/bin/bash
# Run by the agent during the image's /validate hook (VALIDATE_CMD=/app/warm.sh). Lambda records which
# parts of the snapshot the validation touches and prefetches them when a MicroVM starts; without this
# the snapshot disk pages in lazily and the first `claude` in a new sandbox is very slow.
#
# Walks the real cold path: the CLIs, then the Claude Code TUI all the way to its input box, inside tmux.
# The TUI runs with a throwaway HOME (a copy of the baked-in config) so /root/.claude.json and
# /root/.claude/ stay exactly as built. There is no proxy at build time, so Claude Code's API calls fail
# with the fake key; that doesn't matter, the point is to page in the binary and the TUI code.
set -u
cd /root/work
claude --version
tmux -V
git --version
node --version
aws --version
gh --version | head -1
python3 -c 'import json, glob, subprocess'

W=$(mktemp -d /tmp/warm-home.XXXX)
mkdir -p "$W/.claude"
cp /root/.claude.json "$W/.claude.json"
cp /root/.claude/settings.json "$W/.claude/settings.json"
cp /root/.tmux.conf "$W/.tmux.conf"
# A key whose last 20 characters match the pre-approved one, so the TUI skips the API key prompt
KEY="sk-ant-warmup-$(jq -r '.customApiKeyResponses.approved[0]' /root/.claude.json)"

t0=$(date +%s)
HOME="$W" ANTHROPIC_API_KEY="$KEY" tmux -L warm -f "$W/.tmux.conf" new-session -d -x 160 -y 45 -c /root/work claude
for _ in $(seq 1 240); do
  tmux -L warm capture-pane -p 2>/dev/null | grep -q "bypass permissions on" && break
  sleep 0.5
done
echo "claude TUI input box after $(( $(date +%s) - t0 ))s"
tmux -L warm capture-pane -p 2>/dev/null | grep -m2 -E "Claude Code v|bypass permissions"
tmux -L warm kill-server 2>/dev/null
sleep 1
rm -rf "$W"
