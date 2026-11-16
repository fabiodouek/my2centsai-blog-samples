# Tests 3 and 4: credential injection. Stores a fake secret as an environment_variable credential trusted
# only for httpbin.org (in headers), binds MY_TOKEN to it, runs probe3.sh, then deletes the credential.
source lib.sh
CANARY=canary-$(openssl rand -hex 6)   # a fake secret
echo "### Store the secret as a credential: $CANARY"
curl -s "$API/credentials" -H "x-goog-api-key: $GEMINI_API_KEY" -H 'Content-Type: application/json' \
  -d "{\"id\": \"demo-token\", \"type\": \"environment_variable\", \"value\": \"$CANARY\",
       \"trusted_domains\": [\"httpbin.org\"], \"injection_location\": \"header\"}" | jq -c '{id, type, status}'
echo "### Run the agent with MY_TOKEN bound to it"
ask '{"type": "remote", "env": {"MY_TOKEN": {"credential": "demo-token"}},
      "network": {"allowlist": [{"domain": "httpbin.org"}, {"domain": "example.com"}]}}' probe3.sh
curl -s -X DELETE "$API/credentials/demo-token" -H "x-goog-api-key: $GEMINI_API_KEY" -o /dev/null -w "### credential deleted: %{http_code}\n"
