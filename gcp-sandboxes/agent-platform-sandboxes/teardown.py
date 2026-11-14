"""Delete everything this part created in the project, then verify. Run: python teardown.py

Deletes: sandboxes, snapshots and templates (each template holds a warm pool) of every Agent Runtime
instance named sbx-part3, the instances themselves, the Artifact Registry repos sbx-byoc*, the Cloud Build
source tarballs (and the bucket if nothing else is in it), the Computer Use service account and the IAM
bindings this part added, and turns the Data Access audit logs for aiplatform.googleapis.com off again.
"""
import subprocess
from sbx import *

P = PROJECT
SA = os.environ.get("SA_EMAIL", f"sbx-cu@{P}.iam.gserviceaccount.com")


def gc(*args):
    r = subprocess.run(["gcloud", *args, "--project", P, "--quiet"], capture_output=True, text=True)
    print("  gcloud", redact(" ".join(a for a in args if not a.startswith("--role")))[:84], "->", "ok" if r.returncode == 0 else (r.stderr.strip().splitlines() or ["failed"])[-1][:100])
    return r


code, j = rest("GET", "reasoningEngines")
engines = [e for e in j.get("reasoningEngines", []) if e.get("displayName") == "sbx-part3"]
for e in engines:
    rel = "reasoningEngines/" + e["name"].rsplit("/", 1)[-1]
    print("== instance", rel.rsplit("/", 1)[-1])
    for coll in ("sandboxEnvironments", "sandboxEnvironmentSnapshots", "sandboxEnvironmentTemplates"):
        items = rest("GET", f"{rel}/{coll}")[1].get(coll, [])
        for it in items:
            if it.get("state") in ("DELETED", "DEPROVISIONING"):
                continue
            print("  delete", coll[18:], it.get("displayName"), rest("DELETE", it["name"].split("locations/us-central1/")[1])[0])
time.sleep(15)
for e in engines:
    print("  delete instance ->", rest("DELETE", "reasoningEngines/" + e["name"].rsplit("/", 1)[-1] + "?force=true")[0])

print("== Artifact Registry and Cloud Build")
r = subprocess.run(["gcloud", "artifacts", "repositories", "list", "--location=us-central1", "--project", P, "--format=value(name)"], capture_output=True, text=True)
for repo in [x.rsplit("/", 1)[-1] for x in r.stdout.split() if x.rsplit("/", 1)[-1].startswith("sbx-byoc")]:
    gc("artifacts", "repositories", "delete", repo, "--location=us-central1")
gc("storage", "rm", "-r", f"gs://{P}_cloudbuild/source/")
r = subprocess.run(["gcloud", "storage", "ls", f"gs://{P}_cloudbuild/", "--project", P], capture_output=True, text=True)
if not r.stdout.strip():
    gc("storage", "buckets", "delete", f"gs://{P}_cloudbuild")

print("== service account, IAM bindings, audit logs")
num = subprocess.run(["gcloud", "projects", "describe", P, "--format=value(projectNumber)"], capture_output=True, text=True).stdout.strip()
gc("projects", "remove-iam-policy-binding", P, f"--member=serviceAccount:{SA}", "--role=roles/aiplatform.user", "--condition=None")
gc("projects", "remove-iam-policy-binding", P, f"--member=serviceAccount:{num}-compute@developer.gserviceaccount.com", "--role=roles/cloudbuild.builds.builder", "--condition=None")
gc("iam", "service-accounts", "delete", SA)
for attempt in range(5):   # the IAM policy is shared: retry on concurrent changes
    pol = json.loads(subprocess.run(["gcloud", "projects", "get-iam-policy", P, "--format=json"], capture_output=True, text=True).stdout)
    pol["auditConfigs"] = [c for c in pol.get("auditConfigs", []) if c["service"] != "aiplatform.googleapis.com"]   # [] clears it
    open("/tmp/policy-off.json", "w").write(json.dumps(pol))
    if gc("projects", "set-iam-policy", P, "/tmp/policy-off.json").returncode == 0:
        break
    time.sleep(2)

print("\n== verify")
print("  Agent Runtime instances left:", len(rest("GET", "reasoningEngines")[1].get("reasoningEngines", [])))
r = subprocess.run(["gcloud", "artifacts", "repositories", "list", "--location=us-central1", "--project", P, "--format=value(name)"], capture_output=True, text=True)
print("  Artifact Registry repos:", [x.rsplit("/", 1)[-1] for x in r.stdout.split()])
r = subprocess.run(["gcloud", "iam", "service-accounts", "list", "--project", P, "--format=value(email)"], capture_output=True, text=True)
print("  service accounts sbx-*:", [x.split("@")[0] for x in r.stdout.split() if x.startswith("sbx-")])
print("  audit configs:", json.loads(subprocess.run(["gcloud", "projects", "get-iam-policy", P, "--format=json"], capture_output=True, text=True).stdout).get("auditConfigs", "none"))
