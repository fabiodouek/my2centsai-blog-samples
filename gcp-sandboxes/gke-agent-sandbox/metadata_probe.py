"""Prints what the metadata server tells a workload. The project ID and number are replaced with placeholders in the output."""
import urllib.request as u, json, re
def g(p, t=4):
    r = u.Request("http://169.254.169.254/computeMetadata/v1/" + p, headers={"Metadata-Flavor": "Google"})
    try: return u.urlopen(r, timeout=t).read().decode()
    except Exception as e: return "ERR " + str(getattr(e, 'code', e))
project = g("project/project-id")
for p in ["project/project-id", "instance/zone", "instance/name", "instance/hostname", "instance/service-accounts/",
          "instance/service-accounts/default/email", "instance/attributes/cluster-name", "instance/service-accounts/default/token"]:
    v = g(p)
    if p.endswith("token") and not v.startswith("ERR"):
        d = json.loads(v); v = "token ok, type=%s, expires_in=%s, len=%d" % (d.get("token_type"), d.get("expires_in"), len(d["access_token"]))
    if not project.startswith("ERR"):
        v = v.replace(project, "<project>")
    v = re.sub(r"projects/\d+", "projects/<project-number>", v)
    print(p, "->", v.replace("\n", " | ")[:100])
