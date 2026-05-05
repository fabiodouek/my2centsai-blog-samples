"""List all schedules, or describe the interval schedule.

Usage:
    uv run python -m src.schedules.list_describe list
    uv run python -m src.schedules.list_describe describe
"""

import asyncio
import sys

from temporalio.client import Client

from ..config import INTERVAL_SCHEDULE_ID, TEMPORAL_ADDRESS, TEMPORAL_NAMESPACE


async def list_all(client: Client) -> None:
    print(f"{'ID':<32} note")
    print("-" * 80)
    async for s in await client.list_schedules():
        note = s.schedule.state.note if s.schedule and s.schedule.state else ""
        print(f"{s.id:<32} {note}")


async def describe_one(client: Client, schedule_id: str) -> None:
    handle = client.get_schedule_handle(schedule_id)
    desc = await handle.describe()
    info = desc.info
    state = desc.schedule.state
    print(f"id:          {schedule_id}")
    print(f"note:        {state.note}")
    print(f"paused:      {state.paused}")
    print(f"num_actions: {info.num_actions}")
    print(f"running_workflows: {len(info.running_workflows)}")
    if info.recent_actions:
        last = info.recent_actions[-1]
        print(f"last_action_started_at:   {last.start_time}")
        print(f"last_action_scheduled_at: {last.scheduled_time}")


async def main(action: str) -> None:
    client = await Client.connect(TEMPORAL_ADDRESS, namespace=TEMPORAL_NAMESPACE)
    if action == "list":
        await list_all(client)
    elif action == "describe":
        await describe_one(client, INTERVAL_SCHEDULE_ID)
    else:
        print(f"unknown action: {action}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    asyncio.run(main(sys.argv[1]))
