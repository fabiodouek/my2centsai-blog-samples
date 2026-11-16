# Cleanup: deletes every Managed Agents environment for this API key, then lists what's left.
source lib.sh
echo "### Delete every environment"
for e in $(curl -s "$API/environments?pageSize=100" -H "x-goog-api-key: $GEMINI_API_KEY" | jq -r '.environments[]?.id'); do
  curl -s -X DELETE "$API/environments/$e" -H "x-goog-api-key: $GEMINI_API_KEY" -o /dev/null -w "deleted $e: %{http_code}\n"
done
echo "### Left: $(curl -s "$API/environments" -H "x-goog-api-key: $GEMINI_API_KEY" | jq '.environments | length') environments, $(curl -s "$API/credentials" -H "x-goog-api-key: $GEMINI_API_KEY" | jq '.credentials | length') credentials"
