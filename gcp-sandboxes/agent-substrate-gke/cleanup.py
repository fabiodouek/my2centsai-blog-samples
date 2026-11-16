"""Delete every task in the 'stress' atespace, the probe pod, and stop the AX tunnel."""

from common import banner, delete_all, kubectl, list_tasks, stop_tunnel

banner("tasks in atespace 'stress'")
delete_all()
print(f"  remaining: {len(list_tasks())}")
kubectl("-n", "ax-system", "delete", "pod", "router-probe", "--ignore-not-found", quiet=True)
stop_tunnel()
