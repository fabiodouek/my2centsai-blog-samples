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
    print(f"num_actions:                {info.num_actions}")
    print(f"num_actions_skipped_overlap:{info.num_actions_skipped_overlap}")
    print(f"running_actions:            {len(info.running_actions)}")
    if info.recent_actions:
        last = info.recent_actions[-1]
        print(f"last_action_started_at:     {last.started_at}")
        print(f"last_action_scheduled_at:   {last.scheduled_at}")
    if info.next_action_times:
        print(f"next_action_at:             {info.next_action_times[0]}")


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
