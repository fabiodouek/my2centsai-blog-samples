"""T5: Code Execution limits: JavaScript, pip install, file size boundary, output files."""
from sbx import *

rt = runtime_name()
R = {}


def new(lang, name):
    return client.sandboxes.create(name=rt, spec={"code_execution_environment": {"code_language": lang}},
                                   config={"ttl": "3600s", "display_name": name}).response


def mk_bytes(mb):
    return os.urandom(mb * 1024 * 1024)


print("== JavaScript")
sb = new("LANGUAGE_JAVASCRIPT", "t5-js")
out, stderr, files = run(sb.name, "console.log(process.version, typeof require, [1,2,3].map(x => x * 2));")
print("  ", (out + stderr).strip())
R["javascript"] = out + stderr
client.sandboxes.delete(name=sb.name)

print("\n== Python: libraries and pip")
sb = new("LANGUAGE_PYTHON", "t5-py")
out, stderr, _ = run(sb.name, """
import importlib, subprocess, sys
for m in ("numpy", "pandas", "matplotlib", "openpyxl", "scipy", "PIL", "requests"):
    try:
        mod = importlib.import_module(m); print(f"  {m:11s} {getattr(mod, '__version__', 'yes')}")
    except Exception as e:
        print(f"  {m:11s} missing")
print("python", sys.version.split()[0])
r = subprocess.run([sys.executable, "-m", "pip", "install", "cowsay"], capture_output=True, text=True, timeout=60)
print("pip install cowsay -> exit", r.returncode, "|", (r.stdout + r.stderr).strip().splitlines()[-1][:160])
""")
print(out, stderr)
R["python"] = out + stderr

print("== output files: matplotlib PNG and an XLSX")
code = """
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt, openpyxl
plt.plot([1, 2, 3], [2, 4, 8]); plt.title("hello"); plt.savefig("chart.png")
wb = openpyxl.Workbook(); ws = wb.active; ws.append(["a", "b"]); ws.append([1, 2]); wb.save("book.xlsx")
print("saved")
"""
r = client.sandboxes.execute_code(name=sb.name, input_data={"code": code})
R["outputs"] = []
for o in r.outputs:
    fn = (o.metadata.attributes.get("file_name") if o.metadata and o.metadata.attributes else None)
    fn = fn.decode() if isinstance(fn, bytes) else fn
    data = o.data or b""
    kind = o.mime_type
    if fn:
        pth = os.path.join("/tmp", fn)
        open(pth, "wb").write(data)   # data arrives as raw bytes; no base64 decoding needed
    print(f"  chunk mime={kind!s:70s} file={fn} bytes={len(data)}")
    R["outputs"].append({"mime": kind, "file": fn, "bytes": len(data)})
client.sandboxes.delete(name=sb.name)
save_result("t5", R)
