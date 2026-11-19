"""Front a public MCP server (Microsoft Learn) through the gateway, then block one tool."""
import time

import requests

import common

URL = f"https://{common.host()}/default/toolservers/learn/mcp"
H = {"Authorization": f"Bearer {common.key()}", "Content-Type": "application/json",
     "Accept": "application/json, text/event-stream"}


def rpc(i, method, params=None):
    r = requests.post(URL, headers=H, json={"jsonrpc": "2.0", "id": i, "method": method, **({"params": params} if params else {})}, timeout=60)
    return r


def tools():
    rpc(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "demo", "version": "1"}})
    return [t["name"] for t in rpc(2, "tools/list").json()["result"]["tools"]]


common.aig("mcp", "create", "-n", "learn", "--url", "https://learn.microsoft.com/api/mcp", "--namespace", "learn", "-o", "none")
print("tool server 'learn' created; waiting 30 s")
time.sleep(30)
print("tools through the gateway:", tools())
common.aig("mcp", "update", "-n", "learn", "--blocked", "learn_microsoft_docs_fetch", "-o", "none")
print("blocked learn_microsoft_docs_fetch; waiting 30 s")
time.sleep(30)
print("tools after the block:   ", tools())
r = rpc(3, "tools/call", {"name": "learn_microsoft_docs_fetch", "arguments": {"url": "https://learn.microsoft.com/azure/"}})
print("call the blocked tool -> HTTP", r.status_code)
print(r.json()["result"]["content"][0]["text"], "| isError =", r.json()["result"].get("isError"))
