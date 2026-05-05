import os

TEMPORAL_ADDRESS = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
TEMPORAL_NAMESPACE = os.environ.get("TEMPORAL_NAMESPACE", "default")
TASK_QUEUE = os.environ.get("TEMPORAL_TASK_QUEUE", "health-check-queue")
DB_PATH = os.environ.get("DB_PATH", "./incidents.db")

INTERVAL_SCHEDULE_ID = "health-check-interval"
CRON_SCHEDULE_ID = "health-check-cron"
WORKFLOW_ID_PREFIX = "health-check"
