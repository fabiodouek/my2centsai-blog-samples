"""Look up the gateway resource ID of each imported model and save them to .state/models.json."""
import json

import common

out = {}
prov = common.names()["FOUNDRY"]
for m in ("main", "alt"):
    out[f"{prov}/{m}"] = common.aig("model", "show", "--provider-name", prov, "-n", m, "--query", "id", "-o", "tsv")
(common.STATE / "models.json").write_text(json.dumps(out))
print("saved", len(out), "model ids")
