"""T7: Computer Use. Raw CDP first (navigate, screenshot), then the Gemini loop, then noVNC,
then whether the open page survives pause/resume and snapshot/restore."""
import re
from sbx import *
import cu_loop

rt = runtime_name()
R = {}
os.makedirs("results", exist_ok=True)


def page(sb, tok):
    v = cdp(sb, "Runtime.evaluate", {"expression": "JSON.stringify([location.href, document.title])", "returnByValue": True}, tok=tok)
    if not isinstance(v, dict):          # the data plane answers with plain text while it is unreachable
        raise RuntimeError(str(v)[:60])
    return [s.replace("\xa0", " ").replace("  ", " ") for s in json.loads(v["result"]["value"])]


print("== (a) raw CDP: navigate, then screenshot")
sb, t = create_from_template("computer-use", ttl="1800s", display_name="t7-cu")
tok = cu_token()
print(f"  sandbox created in {t:.1f}s; health:", client.sandboxes.send_command(
    http_method="GET", access_token=tok, sandbox_environment=sb, port="8080", path="/").body)
print("  Page.navigate ->", cdp(sb, "Page.navigate", {"url": "https://cloud.google.com/docs"}, tok=tok))
time.sleep(4)
png = cu_loop.shot(sb, tok)
open("results/t7-cdp-screenshot.png", "wb").write(png)
print(f"  Page.captureScreenshot -> {len(png)} bytes PNG (saved to results/t7-cdp-screenshot.png), page: {page(sb, tok)[1]}")
R["cdp"] = {"png_bytes": len(png), "title": page(sb, tok)[1]}

print("\n== (b) Gemini 3.8 Flash computer-use loop, at most 6 steps")
task = "Search the Google Cloud docs for 'Agent Platform sandboxes' and open the first result."
t0 = time.perf_counter()
steps, tokens, final = cu_loop.loop(sb, task, save="results/t7-step")
wall = time.perf_counter() - t0
href, title = page(sb, cu_token())
print(f"  {len(steps)} actions, {tokens} tokens, {wall:.0f}s wall; ended on: {title}")
R["loop"] = {"steps": steps, "tokens": tokens, "wall_seconds": round(wall), "final_text": final, "final_title": title,
             "final_url": re.sub(r"\?.*", "", href)}

print("\n== (c) noVNC")
r = client.sandboxes.send_command(http_method="GET", access_token=tok, sandbox_environment=sb, port="5091", path="/vnc.html")
m = re.search(r"<title>(.*?)</title>", r.body, re.S)
print(f"  GET /vnc.html on port 5091 ({len(r.body)} bytes): <title>{m.group(1).strip() if m else '?'}</title>")
R["novnc"] = {"port": 5091, "bytes": len(r.body), "title": m.group(1).strip() if m else None,
              "container_ports": routing_claims(sb)["container_ports"]}

print("\n== (d) does the open page survive pause / resume and snapshot / restore?")
before = page(sb, tok)
client.sandboxes.pause(name=sb.name)
client.sandboxes.resume(name=sb.name)
after, waited = retry(lambda: page(client.sandboxes.get(name=sb.name), cu_token()), tries=60, delay=5)   # re-read the sandbox: its connection info changes
print(f"  before pause: {before[1][:40]}...\n  after resume: {after[1][:40]}...  (answered {waited}s after resume() returned)")
snap = client.sandboxes.snapshots.create(source_sandbox_environment_name=sb.name, config={"ttl": "1800s", "display_name": "t7-snap"}).response
rs = client.sandboxes.create(name=rt, config={"sandbox_environment_snapshot": snap.name, "ttl": "1800s", "display_name": "t7-restore"}).response
restored, waited2 = retry(lambda: page(client.sandboxes.get(name=rs.name), cu_token()), tries=60, delay=5)
print(f"  restored into a new sandbox: {restored[1][:40]}...  (answered after {waited2}s)")
R["pause_resume"] = {"before": before, "after": after}
R["restore"] = {"page": restored}
for n in (sb.name, rs.name):
    client.sandboxes.delete(name=n)
client.sandboxes.snapshots.delete(name=snap.name)
save_result("t7", R)
