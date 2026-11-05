"""Unit tests must never reach AWS: blank credentials and config before anything creates a boto3 client.
(A test once restarted the real iron-proxy service through a default ECS client.)"""

import os

for k in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN"):
    os.environ.pop(k, None)
os.environ.update(AWS_ACCESS_KEY_ID="testing-not-real", AWS_SECRET_ACCESS_KEY="testing-not-real",
                  AWS_CONFIG_FILE="/dev/null", AWS_SHARED_CREDENTIALS_FILE="/dev/null",
                  AWS_EC2_METADATA_DISABLED="true", AWS_DEFAULT_REGION="us-east-1",
                  # Any request that slips through goes nowhere
                  AWS_ENDPOINT_URL="http://127.0.0.1:9")


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_service_quotas(monkeypatch):
    """App code reads the MicroVM memory quota from Service Quotas; unit tests get the QUOTA_GB fallback without
    building a boto3 client (tests that need a value pass their own client)."""
    import app.quota

    class _Off:
        def get_service_quota(self, **kw):
            raise RuntimeError("no Service Quotas in unit tests")

    monkeypatch.setattr(app.quota.MemoryQuota, "client", property(lambda self: self._client or _Off()))
