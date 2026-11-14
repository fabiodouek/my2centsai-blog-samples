"""T8: the custom container contract. Three images, built with Cloud Build and pushed to Artifact Registry:
http-nonroot (HTTP on 8080, USER 1000), no-http (sleep infinity), root (USER 0, HTTP on 8080)."""
import re
from sbx import *

rt = runtime_name()
REPO = f"us-central1-docker.pkg.dev/{PROJECT}/{os.environ.get('BYOC_REPO', 'sbx-byoc')}"
R = {}


def execute_http(sb_name, uri="/", port="8080"):
    """The generic path: :execute with the request URI and port as chunks (what execute_bash does for /exec)."""
    r = client.sandboxes._execute_code(name=sb_name, inputs=[
        types.Chunk(mime_type="application/x.sandbox-request-uri", data=uri.encode()),
        types.Chunk(mime_type="application/x.sandbox-request-port", data=port.encode())])
    return [(o.mime_type, (o.data or b"")[:300]) for o in r.outputs or []]


def wait_state(name, want=("STATE_RUNNING",), limit=240):
    t0 = time.time()
    while time.time() - t0 < limit:
        s = client.sandboxes.get(name=name).state.name
        if s in want or s.endswith("FAILED"):
            return s, round(time.time() - t0, 1)
        time.sleep(2)
    return s, round(time.time() - t0, 1)


for tag, label in (("http-nonroot", "image 1: HTTP on 8080, non-root user"),
                   ("no-http", "image 2: no HTTP server (sleep infinity)"),
                   ("root", "image 3: USER 0 (root), HTTP on 8080")):
    print(f"\n== {label}")
    body = {"displayName": f"byoc-{tag}",
            "customContainerEnvironment": {
                "customContainerSpec": {"imageUri": f"{REPO}/{tag}:1"},
                "ports": [{"port": 8080, "protocol": "TCP"}],
                "resources": {"requests": {"cpu": "1", "memory": "500Mi"}, "limits": {"cpu": "1", "memory": "500Mi"}}},
            "egressControlConfig": {"internetAccess": False}}
    res = {}
    try:
        tname, secs = create_template_rest(f"byoc-{tag}", body, pool=1)
    except Exception as e:  # noqa: BLE001
        print("  template:", err(e))
        res["template"] = err(e)
        R[tag] = res
        continue
    print(f"  template ACTIVE after {secs}s (warm pool 1)")
    res["template_seconds"] = secs
    t0 = time.perf_counter()
    try:
        op = client.sandboxes.create(name=rt, config={"sandbox_environment_template": tname, "ttl": "900s",
                                                      "display_name": f"t8-{tag}", "wait_for_completion": False})
        # the operation carries no sandbox name: find the newest one
        sb = None
        for _ in range(60):
            sbs = [s for s in client.sandboxes.list(name=rt) if s.display_name == f"t8-{tag}"]
            if sbs:
                sb = sbs[0]
                break
            time.sleep(2)
        state, w = wait_state(sb.name)
        sb = client.sandboxes.get(name=sb.name)   # list() results carry no connection info
        print(f"  create -> {state} after {time.perf_counter()-t0:.1f}s")
        res["state"] = state
        res["create_seconds"] = round(time.perf_counter() - t0, 1)
        if state == "STATE_RUNNING":
            tok = cu_token()
            for attempt in (1, 2):
                try:
                    out = execute_http(sb.name)
                    print("  :execute  -> a response came from the container:", out[0][1].decode(errors="replace").replace("\n", " ")[:80] if out else out)
                    res["execute"] = [(m, d.decode(errors="replace")[:200]) for m, d in out]
                    break
                except Exception as e:  # noqa: BLE001
                    msg = err(e, 700)
                    if "UNIMPLEMENTED" in msg or "501" in msg:
                        # the container answered: http.server does not implement POST, which is what :execute sends
                        print("  :execute  -> the container answered 501 'Unsupported method (POST)': it is reached with POST")
                        res["execute"] = msg
                        break
                    print(f"  :execute  -> ERROR (attempt {attempt}): {re.sub(r'.*Error Details: ', '', msg)[:90]}")
                    res["execute"] = msg
                    if attempt == 1:
                        time.sleep(20)
            try:
                r = client.sandboxes.send_command(http_method="GET", access_token=tok, sandbox_environment=sb, port="8080", path="/")
                print("  send_command GET / ->", re.sub(r"\s+", " ", r.body)[:100])
                res["send_command"] = r.body[:200]
            except Exception as e:  # noqa: BLE001
                print("  send_command GET / -> ERROR", err(e, 150))
                res["send_command"] = err(e, 200)
        client.sandboxes.delete(name=sb.name)
    except Exception as e:  # noqa: BLE001
        print("  create ERROR:", err(e, 400))
        res["create_error"] = err(e, 400)
    client.sandboxes.templates.delete(name=tname)
    R[tag] = res
save_result("t8", R)
