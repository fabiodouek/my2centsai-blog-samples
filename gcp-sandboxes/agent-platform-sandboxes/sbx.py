"""Helpers for the Agent Platform sandbox tests. Use with `python -i sbx.py`.

Auth: the SDK wants Application Default Credentials. To avoid a browser login this
module hands it your gcloud user token instead (gcloud auth print-access-token).
Set GOOGLE_CLOUD_PROJECT, and optionally CLOUDSDK_ACTIVE_CONFIG_NAME, before starting.
"""
import base64, json, os, subprocess, time, datetime
import google.auth, google.auth.credentials
import requests

PROJECT = os.environ["GOOGLE_CLOUD_PROJECT"]
LOCATION = os.environ.get("LOCATION", "us-central1")
API = f"https://{LOCATION}-aiplatform.googleapis.com/v1beta1"


class GcloudCreds(google.auth.credentials.Credentials):
    """Refreshes from `gcloud auth print-access-token` (tokens live one hour)."""

    def refresh(self, request):
        self.token = subprocess.check_output(
            ["gcloud", "auth", "print-access-token"], text=True).strip()
        self.expiry = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) \
            + datetime.timedelta(minutes=50)


CREDS = GcloudCreds()
google.auth.default = lambda *a, **k: (CREDS, PROJECT)   # used by generate_access_token()

import agentplatform
from agentplatform import types

client = agentplatform.Client(project=PROJECT, location=LOCATION, credentials=CREDS,
                              http_options={"api_version": "v1beta1"})


def token():
    if not CREDS.valid:
        CREDS.refresh(None)
    return CREDS.token


def rest(method, path, body=None, timeout=120):
    """Raw v1beta1 REST call. `path` is relative, e.g. 'reasoningEngines/123/sandboxEnvironments'."""
    url = path if path.startswith("http") else f"{API}/projects/{PROJECT}/locations/{LOCATION}/{path}"
    r = requests.request(method, url, json=body, timeout=timeout,
                         headers={"Authorization": f"Bearer {token()}",
                                  "x-goog-user-project": PROJECT})
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, r.text


def timed(fn, *a, **k):
    """Run fn and return (result, seconds)."""
    t = time.perf_counter()
    out = fn(*a, **k)
    return out, round(time.perf_counter() - t, 3)


def runtime_name():
    """The Agent Runtime instance every sandbox lives under (created once, see t0-setup.py)."""
    return open(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".runtime")).read().strip()


def short(name):
    return name.rsplit("/", 1)[-1]


def run(sb, code):
    """Code Execution: run Python (or JavaScript) and return (stdout, stderr, files)."""
    r = client.sandboxes.execute_code(name=sb, input_data={"code": code})
    out, err, files = "", "", []
    for o in r.outputs or []:
        if o.mime_type == "application/json" and not (o.metadata and o.metadata.attributes):
            d = json.loads(o.data)
            out += d.get("msg_out", "")
            err += d.get("msg_err", "")
        else:
            files.append(o)
    return out, err, files


def sh(sb, command, **kw):
    """Shell / custom-container sandbox: run bash, return the dict (stdout, stderr, returncode, duration_ms)."""
    return client.sandboxes.execute_bash(name=sb, command=command, **kw)


def delete_all():
    """Delete every sandbox under the runtime. Use after every session."""
    for s in client.sandboxes.list(name=runtime_name()):
        print("delete", s.name)
        client.sandboxes.delete(name=s.name)


# ---- templates -------------------------------------------------------------------------
import threading
from agentplatform import types as _t

_CAT = _t.DefaultContainerCategory
TEMPLATES_FILE = os.path.join(os.path.dirname(__file__), ".templates.json")
TEMPLATE_SPECS = {
    # name: (default container category, internet_access)
    "shell-net-off": (_CAT.DEFAULT_CONTAINER_CATEGORY_SHELL_SANDBOX, False),
    "shell-net-on": (_CAT.DEFAULT_CONTAINER_CATEGORY_SHELL_SANDBOX, True),
    "computer-use": (_CAT.DEFAULT_CONTAINER_CATEGORY_COMPUTER_USE, True),
}


def saved_templates():
    try:
        return json.load(open(TEMPLATES_FILE))
    except FileNotFoundError:
        return {}


def save_template(display_name, name):
    d = saved_templates()
    d[display_name] = name
    json.dump(d, open(TEMPLATES_FILE, "w"), indent=1)


def create_template(display_name, category, internet, pool=None):
    """Create a Shell or Computer Use template and poll until ACTIVE. Returns (name, seconds_to_active).
    pool: warm pool size. The SDK has no argument for it, but the REST field warmPoolConfig works;
    left as None the service picks its default (10)."""
    body = {"displayName": display_name,
            "defaultContainerEnvironment": {"defaultContainerCategory": category.name},
            "egressControlConfig": {"internetAccess": internet}}
    return create_template_rest(display_name, body, pool)


def create_template_rest(display_name, body, pool=None):
    """POST a template (camelCase REST body), then find it by display name and wait for ACTIVE."""
    if pool is not None:
        body = {**body, "warmPoolConfig": {"targetInstanceCount": pool}}
    known = {tp.name for tp in client.sandboxes.templates.list(name=runtime_name())}
    t0 = time.perf_counter()
    code, j = rest("POST", runtime_name().split("locations/us-central1/")[1] + "/sandboxEnvironmentTemplates", body)
    if code != 200:
        raise RuntimeError(f"template create failed: {code} {j}")
    tpl_name, state = None, None
    while True:
        for tp in client.sandboxes.templates.list(name=runtime_name()):
            if tp.display_name == display_name and tp.name not in known:
                tpl_name, state = tp.name, tp.state.name
        if tpl_name and state == "ACTIVE":
            break
        if tpl_name and state in ("FAILED", "DELETED"):
            raise RuntimeError(f"template {display_name} -> {state}")
        time.sleep(3)
    secs = round(time.perf_counter() - t0, 1)
    save_template(display_name, tpl_name)
    return tpl_name, secs


def create_templates_parallel(names=None):
    """Create the standard templates at the same time. Returns {display_name: (name, seconds)}."""
    names = names or list(TEMPLATE_SPECS)
    out, errs = {}, {}

    def work(n):
        cat, net = TEMPLATE_SPECS[n]
        try:
            out[n] = create_template(n, cat, net)
        except Exception as e:  # noqa: BLE001
            errs[n] = repr(e)

    ths = [threading.Thread(target=work, args=(n,)) for n in names]
    [t.start() for t in ths]
    [t.join() for t in ths]
    if errs:
        raise RuntimeError(errs)
    return out


def template(display_name):
    return saved_templates()[display_name]


def create_from_template(tname, ttl="900s", **cfg):
    """Create a sandbox from a saved template. Returns (sandbox, seconds)."""
    op, t = timed(client.sandboxes.create, name=runtime_name(),
                  config={"sandbox_environment_template": template(tname), "ttl": ttl,
                          "display_name": cfg.pop("display_name", tname), **cfg})
    return op.response, t


def delete_templates():
    for tp in client.sandboxes.templates.list(name=runtime_name()):
        print("delete template", tp.display_name)
        try:
            client.sandboxes.templates.delete(name=tp.name)
        except Exception as e:  # noqa: BLE001
            print("  ", repr(e)[:300])
    if os.path.exists(TEMPLATES_FILE):
        os.remove(TEMPLATES_FILE)


import re


def redact(text):
    """Hide the project id, the project number and JWTs before anything is printed or saved."""
    text = str(text).replace(PROJECT, "PROJECT_ID")
    text = re.sub(r"projects/\d+", "projects/PROJECT_NUMBER", text)
    text = re.sub(r"eyJ[\w-]{20,}\.[\w-]{20,}\.[\w-]{20,}", "<jwt>", text)
    text = re.sub(r"service-\d+@", "service-PROJECT_NUMBER@", text)
    return re.sub(r"\b\d{12}\b", "PROJECT_NUMBER", text)


def err(e, n=300):
    """One-line, redacted description of an exception, for printing."""
    return redact(f"{type(e).__name__}: {e}").replace("\n", " ")[:n]


def save_result(name, data):
    """Write raw results of a test to results/<name>.json (committed with the code), redacted."""
    d = os.path.join(os.path.dirname(__file__), "results")
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, name + ".json"), "w").write(redact(json.dumps(data, indent=1, default=str)))


def retry(fn, tries=12, delay=3):
    """Call fn until it stops raising (a resumed sandbox needs a few seconds before its data plane answers).
    Returns (result, seconds_waited)."""
    t0 = time.perf_counter()
    for i in range(tries):
        try:
            return fn(), round(time.perf_counter() - t0, 1)
        except Exception:  # noqa: BLE001
            if i == tries - 1:
                raise
            time.sleep(delay)


def p50(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2]


def routing_claims(sb):
    """The routing token on a sandbox is a JWT. Its claims say what size the platform gave it.
    Returns only the harmless fields (never the token itself)."""
    tok = sb.connection_info.routing_token
    payload = tok.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    keep = ("sandbox_type", "machine_vcpu", "machine_memory_gib", "container_ports", "authz_mode")
    return {k: claims[k] for k in keep if k in claims}


# ---- Computer Use data plane (CDP over the sandbox's own HTTPS endpoint) -------------------
def cu_token():
    return client.sandboxes.generate_access_token(os.environ["SA_EMAIL"])


def cdp(sb, command, params=None, port="8080", tok=None):
    """One Chrome DevTools Protocol command through POST /cdp. Returns the parsed JSON body."""
    r = client.sandboxes.send_command(
        http_method="POST", access_token=tok or cu_token(), sandbox_environment=sb, port=port,
        path="/cdp", request_dict={"command": command, "params": params or {}})
    try:
        return json.loads(r.body)
    except ValueError:
        return r.body
