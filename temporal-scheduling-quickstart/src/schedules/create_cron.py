"""Create a calendar/cron schedule with a timezone.

Demonstrates:
  * ScheduleCalendarSpec with ScheduleRange
  * time_zone_name (IANA: America/New_York)
  * an alternative cron_expressions form (commented) for readers used to crontab
"""

import asyncio

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleCalendarSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleRange,
    ScheduleSpec,
    ScheduleState,
)

from ..config import (
    CRON_SCHEDULE_ID,
    TASK_QUEUE,
    TEMPORAL_ADDRESS,
    TEMPORAL_NAMESPACE,
    WORKFLOW_ID_PREFIX,
)
from ..workflows import HealthCheckWorkflow


async def main() -> None:
    client = await Client.connect(TEMPORAL_ADDRESS, namespace=TEMPORAL_NAMESPACE)

    weekday_9am = ScheduleCalendarSpec(
        hour=(ScheduleRange(start=9),),
        minute=(ScheduleRange(start=0),),
        # Mon..Fri, ScheduleRange uses 0=Sunday in temporal calendar specs
        day_of_week=(ScheduleRange(start=1, end=5),),
        comment="Weekdays 09:00 America/New_York",
    )

    schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            HealthCheckWorkflow.run,
            id=f"{WORKFLOW_ID_PREFIX}-cron",
            task_queue=TASK_QUEUE,
        ),
        spec=ScheduleSpec(
            calendars=[weekday_9am],
            # Equivalent cron_expressions form (uncomment to swap):
            # cron_expressions=["0 9 * * MON-FRI"],
            time_zone_name="America/New_York",
        ),
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.BUFFER_ONE),
        state=ScheduleState(note="Weekday morning health check."),
    )

    try:
        await client.create_schedule(CRON_SCHEDULE_ID, schedule)
        print(f"created schedule: {CRON_SCHEDULE_ID}")
    except Exception as e:
        if "already exists" in str(e).lower() or "alreadyexists" in str(e).lower():
            print(f"schedule {CRON_SCHEDULE_ID} already exists; skipping create")
        else:
            raise


if __name__ == "__main__":
    asyncio.run(main())
