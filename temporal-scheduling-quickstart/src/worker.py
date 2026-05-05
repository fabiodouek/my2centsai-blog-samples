import asyncio
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from .activities import probe_endpoint, record_incident
from .config import TASK_QUEUE, TEMPORAL_ADDRESS, TEMPORAL_NAMESPACE
from .workflows import HealthCheckWorkflow


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    client = await Client.connect(TEMPORAL_ADDRESS, namespace=TEMPORAL_NAMESPACE)
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[HealthCheckWorkflow],
        activities=[probe_endpoint, record_incident],
    )
    print(f"worker connected to {TEMPORAL_ADDRESS} on task_queue={TASK_QUEUE!r}")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
