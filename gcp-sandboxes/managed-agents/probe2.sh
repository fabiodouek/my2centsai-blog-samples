# probe2.sh: runs inside the sandbox. Calls an API, another website and a raw IP address, and prints the
# HTTP status plus the proxy's "Forbidden" reason when a request is blocked.
for u in https://api.github.com https://example.com https://1.1.1.1; do
  code=$(curl -s -m 10 -o /tmp/b -w '%{http_code}' $u)
  echo "$u -> $code $(grep -o 'Forbidden.*' /tmp/b | head -c 60)"
done
