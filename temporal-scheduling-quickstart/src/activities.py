import time
from dataclasses import dataclass

import httpx
from temporalio import activity
from temporalio.exceptions import ApplicationError

from .storage import init_db, insert_incident
from .targets import Target


@dataclass
class ProbeResult:
    target_name: str
    target_url: str
    status_code: int
    elapsed_ms: int


@activity.defn
async def probe_endpoint(target: Target) -> ProbeResult:
    start = time.perf_counter()
    async with httpx.AsyncClient(timeout=target.timeout_seconds) as http:
        resp = await http.get(target.url)
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    activity.logger.info(
        f"probe {target.name}: status={resp.status_code} elapsed_ms={elapsed_ms}"
    )
    if resp.status_code != target.expected_status:
        raise ApplicationError(
            f"unexpected status {resp.status_code} (expected {target.expected_status}) "
            f"from {target.url}",
            type="UnexpectedStatus",
        )
    return ProbeResult(
        target_name=target.name,
        target_url=target.url,
        status_code=resp.status_code,
        elapsed_ms=elapsed_ms,
    )


@activity.defn
async def record_incident(
    target_name: str,
    target_url: str,
    status_code: int | None,
    error: str | None,
    elapsed_ms: int,
) -> None:
    await init_db()
    await insert_incident(target_name, target_url, status_code, error, elapsed_ms)
    activity.logger.warning(
        f"incident recorded: {target_name} status={status_code} error={error!r}"
    )
