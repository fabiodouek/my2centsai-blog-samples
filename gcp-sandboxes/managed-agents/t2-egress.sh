# Test 2: egress control. Runs probe2.sh under the default network, an allowlist of one host, and network disabled.
source lib.sh
echo "### 1. Default network"
ask '{"type": "remote"}' probe2.sh
echo "### 2. Allowlist: api.github.com only"
ask '{"type": "remote", "network": {"allowlist": [{"domain": "api.github.com"}]}}' probe2.sh
echo "### 3. Network disabled"
ask '{"type": "remote", "network": "disabled"}' probe2.sh
