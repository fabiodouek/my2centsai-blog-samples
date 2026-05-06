"""Delete both schedules created by this project."""

import asyncio

from temporalio.client import Client

from ..config import (
    CRON_SCHEDULE_ID,
    INTERVAL_SCHEDULE_ID,
    TEMPORAL_ADDRESS,
    TEMPORAL_NAMESPACE,
)


async def main() -> None:
    client = await Client.connect(TEMPORAL_ADDRESS, namespace=TEMPORAL_NAMESPACE)
    for schedule_id in (INTERVAL_SCHEDULE_ID, CRON_SCHEDULE_ID):
        try:
            await client.get_schedule_handle(schedule_id).delete()
            print(f"deleted {schedule_id}")
        except Exception as e:
            print(f"could not delete {schedule_id}: {e}")


if __name__ == "__main__":
    asyncio.run(main())
