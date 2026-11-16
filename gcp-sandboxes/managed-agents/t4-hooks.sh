# Test 5: hooks. A pre_tool_execution gate that denies rm -rf, run three times: with the docs' matcher
# (code_execution), with the tool name the runtime uses (execute_code), and with a gate that crashes.
source lib.sh
GATE='import sys, json
cmd = str(json.load(sys.stdin).get("tool_call", {}).get("args", {}))
print(json.dumps({"decision": "deny", "reason": "Destructive command blocked."} if "rm -rf" in cmd else {"decision": "allow"}))'
env_with() {  # env_with <matcher> <gate script>
  jq -n --arg m "$1" --arg g "$2" '{type: "remote", sources: [
    {type: "inline", target: ".agents/hooks.json", content: ({gate: {pre_tool_execution: [{matcher: $m,
      hooks: [{type: "command", command: "python3 /.agents/gate.py", timeout: 10}]}]}} | tojson)},
    {type: "inline", target: ".agents/gate.py", content: $g}]}'
}
CMD='mkdir -p /tmp/x && rm -rf /tmp/x && echo "rm ran"'
echo '### 1. The docs example: matcher "code_execution"'
say "$(env_with code_execution "$GATE")" "$CMD"
echo '### 2. Matcher "execute_code", the name the runtime uses'
say "$(env_with execute_code "$GATE")" "$CMD"
echo '### 3. A gate that crashes'
say "$(env_with execute_code 'import sys; sys.exit(1)')" "$CMD"
