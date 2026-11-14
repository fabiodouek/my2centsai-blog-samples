"""T5b: the documented 100 MB cap on input files: upload 99 MB and 101 MB to Code Execution sandboxes.
Slow from a home connection (about nine minutes each, in parallel), so it is not part of the recorded run."""
import threading
from sbx import *

rt = runtime_name()
R = {"upload": {}}


def upload(mb):
    s = client.sandboxes.create(name=rt, spec={"code_execution_environment": {"code_language": "LANGUAGE_PYTHON"}},
                                config={"ttl": "3600s", "display_name": f"t5-upload-{mb}"}).response
    data = os.urandom(mb * 1024 * 1024)
    t0 = time.perf_counter()
    try:
        r = client.sandboxes.execute_code(name=s.name, input_data={
            "code": "import os\nfor f in os.listdir('.'):\n    print(f, os.path.getsize(f))",
            "files": [{"name": f"blob{mb}.bin", "mimeType": "application/octet-stream", "content": data}]})
        res = "ok: " + "".join(json.loads(o.data).get("msg_out", "") for o in r.outputs if o.mime_type == "application/json").strip()
    except Exception as e:  # noqa: BLE001
        res = "ERROR " + err(e, 300)
    res += f"  [{time.perf_counter()-t0:.0f}s]"
    print(f"  {mb} MB -> {res}", flush=True)
    R["upload"][mb] = res
    client.sandboxes.delete(name=s.name)


ths = [threading.Thread(target=upload, args=(mb,)) for mb in (99, 101)]
[x.start() for x in ths]
[x.join() for x in ths]
save_result("t5b", R)
