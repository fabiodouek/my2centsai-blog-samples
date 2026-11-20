#!/usr/bin/env bash
# On the instance, as root: did the kernel kill anything for lack of memory since boot?
# (The agent user cannot read the kernel log, so this runs separately, as root.)
echo "== kernel log lines about out-of-memory kills since boot: $(journalctl -k --no-pager 2>/dev/null | grep -ci -E 'out of memory|oom-kill|killed process')"
