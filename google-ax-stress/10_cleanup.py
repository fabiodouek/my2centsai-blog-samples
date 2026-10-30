"""Delete every task in the 'stress' atespace and turn Container Insights off
again (the add-on bills per metric observation while it runs)."""

from common import CONTEXT, INSTALL, REGION, banner, delete_all, kubectl, list_tasks, run, stop_tunnel

banner("tasks in atespace 'stress'")
delete_all()
print(f"  remaining: {len(list_tasks())}")
kubectl("-n", "ax-system", "delete", "pod", "router-probe", "--ignore-not-found", quiet=True)

banner("terraform apply -var container_insights=false")
p = run(["terraform", f"-chdir={INSTALL / 'terraform'}", "apply", "-input=false", "-auto-approve",
         "-no-color", "-var", "container_insights=false"], quiet=True)
print(" ", [l for l in p.stdout.splitlines() if l.startswith("Apply complete")][0])
# The agent's log groups outlive the add-on.
for group in ("performance", "application", "dataplane", "host"):
    run(["aws", "logs", "delete-log-group", "--region", REGION,
         "--log-group-name", f"/aws/containerinsights/{CONTEXT}/{group}"], quiet=True, check=False)
stop_tunnel()
