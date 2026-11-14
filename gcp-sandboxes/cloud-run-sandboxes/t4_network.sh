# Test 4: network. Runs ON the instance (./run.sh t4_network.sh).
SB=/usr/local/gcp/bin/sandbox
row() { printf '%-34s %s\n' "$1" "$2"; }

echo "=== default: no egress"
row "curl https://www.google.com"   "$($SB do -- /usr/bin/curl -m5 -sS -o /dev/null -w 'HTTP %{http_code}' https://www.google.com 2>&1 | head -1)"
row "getent hosts www.google.com"   "$($SB do -- /usr/bin/getent hosts www.google.com 2>&1 | head -1)"
row "curl http://127.0.0.1:8080/ (the host's own server)" "$($SB do -- /usr/bin/curl -m3 -sS http://127.0.0.1:8080/ 2>&1 | head -1)"

echo; echo "=== --allow-egress"
row "curl https://www.google.com"   "$($SB do --allow-egress -- /usr/bin/curl -m5 -sS -o /dev/null -w 'HTTP %{http_code}' https://www.google.com 2>&1 | head -1)"
row "getent hosts www.google.com"   "$($SB do --allow-egress -- /usr/bin/getent hosts www.google.com 2>&1 | head -1)"
row "curl storage.googleapis.com"   "$($SB do --allow-egress -- /usr/bin/curl -m5 -sS -o /dev/null -w 'HTTP %{http_code}' https://storage.googleapis.com 2>&1 | head -1)"
row "curl http://127.0.0.1:8080/ (the host's own server)" "$($SB do --allow-egress -- /usr/bin/curl -m3 -sS http://127.0.0.1:8080/ 2>&1 | head -1)"
row "curl the service's own URL, no token" "$($SB do --allow-egress -- /usr/bin/curl -m5 -sS -o /dev/null -w 'HTTP %{http_code}' $SERVICE_URL/ 2>&1 | head -1)"
row "same URL from the host, no token" "HTTP $(curl -m5 -s -o /dev/null -w '%{http_code}' $SERVICE_URL/)"
