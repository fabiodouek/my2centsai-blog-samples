"""Entra ID instead of the key: a management token (401), a gateway token with no role (403),
then the AI Gateway User role, and how long until the same call works."""
import subprocess
import time

from azure.identity import AzureCliCredential

import common

ROLE = "caad7a21-134e-43f4-a550-c5ffc17e051c"  # AI Gateway User (data-plane runtime role)
cred = AzureCliCredential()


def attempt(label, scope):
    r, dt = common.call(token=cred.get_token(scope).token, prompt="Say hi in three words.")
    print(f"{label:28s} HTTP {r.status_code} in {dt:.1f}s  {common.short(r, 80)}")
    return r.status_code


attempt("ARM token", "https://management.azure.com/.default")
attempt("gateway token, no role yet", "https://ai.gateway.azure.com/.default")
me = common.az("ad", "signed-in-user", "show", "--query", "id", "-o", "tsv")
gw_id = common.az("aigateway", "show", "-g", common.RG, "-n", common.gateway_name(), "--query", "id", "-o", "tsv")
common.az("role", "assignment", "create", "--assignee-object-id", me, "--assignee-principal-type", "User",
          "--role", ROLE, "--scope", gw_id, "-o", "none")
t0 = time.time()
print("role AI Gateway User assigned at the gateway scope")
while time.time() - t0 < 300:
    if attempt(f"gateway token, +{time.time()-t0:3.0f}s", "https://ai.gateway.azure.com/.default") == 200:
        print(f"role took about {time.time()-t0:.0f}s to take effect")
        break
    time.sleep(10)
