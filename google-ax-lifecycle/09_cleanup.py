"""Delete every task in the 'lifecycle' atespace and show what's left in S3."""

from common import ATESPACE, ax, banner, list_tasks, s3_summary, stop_tunnel, summary, timed

banner(f"delete every task in atespace '{ATESPACE}'")
times = []
while names := list_tasks():  # `ax get tasks` lists at most 50
    for name in names:
        _, ms = timed(f"ax delete {name}", ax, "delete", "task", name, check=False, quiet=True)
        times.append(ms)
if times:
    summary("delete", times)
ax("get", "tasks")

banner("snapshot bucket")
print(" ", s3_summary())
stop_tunnel()
