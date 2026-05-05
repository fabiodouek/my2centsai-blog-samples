"""Create the every-2-minute interval schedule.

Demonstrates:
  * ScheduleIntervalSpec with `every` and a small offset
  * jitter so concurrent instances spread out
  * SKIP overlap policy: don't fire if the previous run is still going
  * note: this is the boring, durable workhorse most teams want
"""

import asyncio
from datetime import timedelta

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
    ScheduleState,
)

from ..config import (
    INTERVAL_SCHEDULE_ID,
    TASK_QUEUE,
    TEMPORAL_ADDRESS,
    TEMPORAL_NAMESPACE,
    WORKFLOW_ID_PREFIX,
)
from ..workflows import HealthCheckWorkflow


async def main() -> None:
    client = await Client.connect(TEMPORAL_ADDRESS, namespace=TEMPORAL_NAMESPACE)

    schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            HealthCheckWorkflow.run,
            id=f"{WORKFLOW_ID_PREFIX}-interval",
            task_queue=TASK_QUEUE,
        ),
        spec=ScheduleSpec(
            intervals=[ScheduleIntervalSpec(every=timedelta(minutes=2))],
            jitter=timedelta(seconds=10),
        ),
        policy=SchedulePolicy(
            overlap=ScheduleOverlapPolicy.SKIP,
            catchup_window=timedelta(minutes=10),
        ),
        state=ScheduleState(note="Health check every 2 minutes with 10s jitter."),
    )

    try:
        await client.create_schedule(INTERVAL_SCHEDULE_ID, schedule)
        print(f"created schedule: {INTERVAL_SCHEDULE_ID}")
    except Exception as e:
        if "already exists" in str(e).lower() or "alreadyexists" in str(e).lower():
            print(f"schedule {INTERVAL_SCHEDULE_ID} already exists; skipping create")
        else:
            raise


if __name__ == "__main__":
    asyncio.run(main())
