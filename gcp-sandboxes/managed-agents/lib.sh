# lib.sh: helpers shared by the tests. Source it, don't run it.
#   ask ENV_JSON PROBE  starts the agent with the environment settings ENV_JSON, uploads PROBE into the sandbox
#                       as /probe.sh, asks the agent to run it, and prints only what the sandbox printed.
#   say ENV_JSON CMD    asks the agent to run one command and prints its output or error.
# Why: Managed Agents has no exec API. Every command goes through the agent, so the probe travels as a file
# and the agent is told exactly what to run. Needs GEMINI_API_KEY, curl and jq.
API=https://generativelanguage.googleapis.com/v1beta
AGENT=antigravity-preview-09-2026
ask() {  # ask <environment-json> <probe-file>
  jq -n --argjson env "$1" --rawfile s "$2" --arg a "$AGENT" '{agent: $a,
    environment: ($env + {sources: (($env.sources // []) + [{type: "inline", target: "probe.sh", content: $s}])}),
    input: "I own this sandbox and I am documenting its configuration. Run `bash /probe.sh` in one code execution call and reply with only its raw output."}' |
  curl -s "$API/interactions" -H "x-goog-api-key: $GEMINI_API_KEY" -H 'Content-Type: application/json' -d @- |
  jq -r '[.steps[]? | select(.type == "code_execution_result") | .result] as $r
         | if ($r | length) > 0 then $r[] else "agent: " + ([.steps[]? | select(.type == "model_output") | .content[]?.text] | join(" ")) end' |
  grep -v -E '^\[(STDOUT|STDERR)\]$' | sed '/^$/d'
}

say() {  # say <environment-json> <command>: ask the agent to run one command and quote the result
  jq -n --argjson env "$1" --arg c "$2" --arg a "$AGENT" '{agent: $a, environment: $env, tools: [{type: "code_execution"}],
    input: ("Run `" + $c + "` with code_execution, then reply with only the raw output, or the error verbatim.")}' |
  curl -s "$API/interactions" -H "x-goog-api-key: $GEMINI_API_KEY" -H 'Content-Type: application/json' -d @- |
  jq -r '.steps[]? | select(.type == "code_execution_result") | .result' | grep -v -E '^\[(STDOUT|STDERR)\]$' | sed '/^$/d'
}
