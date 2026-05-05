"""Pause / unpause / trigger the interval schedule.

Usage:
    uv run python -m src.schedules.pause_trigger pause
    uv run python -m src.schedules.pause_trigger unpause
    uv run python -m src.schedules.pause_trigger trigger
"""

import asyncio
import sys

from temporalio.client import Client, ScheduleOverlapPolicy

from ..config import INTERVAL_SCHEDULE_ID, TEMPORAL_ADDRESS, TEMPORAL_NAMESPACE


async def main(action: str) -> None:
    client = await Client.connect(TEMPORAL_ADDRESS, namespace=TEMPORAL_NAMESPACE)
    handle = client.get_schedule_handle(INTERVAL_SCHEDULE_ID)

    if action == "pause":
        await handle.pause(note="Paused via pause_trigger.py")
        print(f"paused {INTERVAL_SCHEDULE_ID}")
    elif action == "unpause":
        await handle.unpause(note="Unpaused via pause_trigger.py")
        print(f"unpaused {INTERVAL_SCHEDULE_ID}")
    elif action == "trigger":
        await handle.trigger(overlap=ScheduleOverlapPolicy.ALLOW_ALL)
        print(f"triggered ad-hoc run of {INTERVAL_SCHEDULE_ID}")
    else:
        print(f"unknown action: {action}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    asyncio.run(main(sys.argv[1]))
