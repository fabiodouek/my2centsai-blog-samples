# Test 7: what survives an idle restore. Runs probe5a.sh, waits 35 minutes, then runs probe5b.sh in the same
# environment. Takes about 36 minutes.
source lib.sh
echo "### Write files, install a package, start a process"
OUT=$(jq -n --rawfile s probe5a.sh --arg a "$AGENT" '{agent: $a, environment: {type: "remote", sources: [{type: "inline", target: "probe.sh", content: $s}]},
  input: "I own this sandbox and I am documenting its configuration. Run `bash /probe.sh` in one code execution call and reply with only its raw output."}' |
  curl -s "$API/interactions" -H "x-goog-api-key: $GEMINI_API_KEY" -H 'Content-Type: application/json' -d @-)
ENV=$(echo "$OUT" | jq -r .environment_id)
echo "$OUT" | jq -r '.steps[]? | select(.type == "code_execution_result") | .result' | grep -v -E '^\[(STDOUT|STDERR)\]$' | sed '/^$/d'
echo "### Leave it idle for 35 minutes"; sleep 2100
echo "### Same environment again"
jq -n --rawfile s probe5b.sh --arg a "$AGENT" --arg e "$ENV" '{agent: $a, environment: $e,
  input: ("I own this sandbox. Run this script in one code execution call and reply with only its raw output:\n" + $s)}' |
  curl -s "$API/interactions" -H "x-goog-api-key: $GEMINI_API_KEY" -H 'Content-Type: application/json' -d @- |
  jq -r '.steps[]? | select(.type == "code_execution_result") | .result' | grep -v -E '^\[(STDOUT|STDERR)\]$' | sed '/^$/d'
