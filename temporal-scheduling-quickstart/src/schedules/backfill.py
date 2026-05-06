"""Backfill the interval schedule for the previous 30 minutes.

Demonstrates ScheduleBackfill with ALLOW_ALL overlap so every missed run
fires concurrently rather than queuing."""

import asyncio
from datetime import datetime, timedelta, timezone

from temporalio.client import Client, ScheduleBackfill, ScheduleOverlapPolicy

from ..config import INTERVAL_SCHEDULE_ID, TEMPORAL_ADDRESS, TEMPORAL_NAMESPACE


async def main() -> None:
    client = await Client.connect(TEMPORAL_ADDRESS, namespace=TEMPORAL_NAMESPACE)
    handle = client.get_schedule_handle(INTERVAL_SCHEDULE_ID)

    now = datetime.now(timezone.utc)
    await handle.backfill(
        ScheduleBackfill(
            start_at=now - timedelta(minutes=30),
            end_at=now - timedelta(minutes=1),
            overlap=ScheduleOverlapPolicy.ALLOW_ALL,
        ),
    )
    print(f"backfilled {INTERVAL_SCHEDULE_ID} for the last 30 minutes")


if __name__ == "__main__":
    asyncio.run(main())
