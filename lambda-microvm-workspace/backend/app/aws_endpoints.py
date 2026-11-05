"""boto3 client -> SigV4 signing name, endpoint prefix and endpoint host in one Region, from botocore's service
models and endpoint rules. iron-proxy's aws_auth.allowed_services needs signing names (CloudWatch signs as
`monitoring`, SES as `ses` on `email.<region>.amazonaws.com`), and the allowlist needs hosts.

Creating every client takes about 2 s but holds ~500 MB, too much for the API task, so the map is generated
once, at image build time, from the botocore the image installs:

    python -m app.aws_endpoints > app/data/aws_endpoints.json

and read at runtime with load()."""

from __future__ import annotations

import json
import os
import sys
from functools import cache
from pathlib import Path
from urllib.parse import urlparse

DATA = Path(__file__).parent / "data" / "aws_endpoints.json"


def generate(region: str = "us-east-1") -> dict:
    import botocore
    import botocore.session
    from botocore import UNSIGNED
    from botocore.config import Config

    s = botocore.session.get_session()
    clients, failed = {}, {}
    for name in s.get_available_services():
        try:
            c = s.create_client(name, region_name=region, config=Config(signature_version=UNSIGNED))
            m = c.meta.service_model
            clients[name] = {"signing_name": m.signing_name, "endpoint_prefix": m.endpoint_prefix,
                             "host": urlparse(c.meta.endpoint_url).hostname,
                             "full_name": m.metadata.get("serviceFullName") or m.service_name}
        except Exception as e:  # pragma: no cover - a client botocore can't build here
            failed[name] = f"{type(e).__name__}: {str(e)[:120]}"
    return {"botocore": botocore.__version__, "region": region, "clients": clients, "failed": failed}


@cache
def load(path: str | None = None) -> dict:
    return json.loads(Path(path or DATA).read_text())


if __name__ == "__main__":
    json.dump(generate(os.environ.get("AWS_REGION") or "us-east-1"), sys.stdout, indent=0, sort_keys=True)
    sys.stdout.write("\n")
