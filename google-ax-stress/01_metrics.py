"""Step 1: turn on Container Insights (the Amazon CloudWatch Observability
add-on) for the test window, and check the agent is running.

Metrics only: container logs and Application Signals are off (see
../google-ax-eks/terraform/main.tf). Remove it afterwards with 10_cleanup.py."""

import time

from common import INSTALL, banner, kubectl, run, save

banner("terraform apply -var container_insights=true")
t0 = time.time()
p = run(["terraform", f"-chdir={INSTALL / 'terraform'}", "apply", "-input=false", "-auto-approve",
         "-no-color", "-var", "container_insights=true"], quiet=True)
for line in p.stdout.splitlines():
    if "Creation complete" in line or line.startswith("Apply complete"):
        print(" ", line.split(": ", 1)[-1] if "Creation complete" in line else line)
print(f"  took {time.time() - t0:.0f} s")

banner("the agent: one pod per node")
kubectl("-n", "amazon-cloudwatch", "rollout", "status", "daemonset/cloudwatch-agent", "--timeout=300s", check=False)
kubectl("-n", "amazon-cloudwatch", "get", "pods", "-o", "wide")

save("window", {"start": time.time()})  # 08_cloudwatch.py pulls metrics from here to now
