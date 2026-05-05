from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from .activities import probe_endpoint, record_incident
    from .targets import TARGETS


@workflow.defn
class HealthCheckWorkflow:
    """Probe every target once. Each probe is an activity with its own retries.

    If a probe ultimately fails, we record an incident in SQLite via a separate
    activity. The workflow itself never raises — the schedule should keep firing
    even if a target is down."""

    @workflow.run
    async def run(self) -> dict[str, str]:
        retry_policy = RetryPolicy(
            initial_interval=timedelta(seconds=1),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(seconds=10),
            maximum_attempts=3,
            non_retryable_error_types=["UnexpectedStatus"],
        )
        results: dict[str, str] = {}
        for target in TARGETS:
            try:
                outcome = await workflow.execute_activity(
                    probe_endpoint,
                    target,
                    start_to_close_timeout=timedelta(seconds=15),
                    retry_policy=retry_policy,
                )
                results[target.name] = f"ok status={outcome.status_code}"
            except Exception as e:
                msg = str(e)
                workflow.logger.warning(f"{target.name} failed: {msg}")
                await workflow.execute_activity(
                    record_incident,
                    args=[target.name, target.url, None, msg, 0],
                    start_to_close_timeout=timedelta(seconds=10),
                    retry_policy=RetryPolicy(maximum_attempts=2),
                )
                results[target.name] = f"incident: {msg}"
        return results
