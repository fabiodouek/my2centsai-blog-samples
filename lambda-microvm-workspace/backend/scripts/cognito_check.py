"""Production-mode auth check against the real Cognito pool (no DEV_AUTH): a throwaway user signs in with
USER_PASSWORD_AUTH, the API (uvicorn locally, ORIGIN_SECRET set, DynamoDB table) must accept its ID token and
reject the access token, a missing origin header and garbage. The user is deleted at the end.

  cd backend && uv run python scripts/cognito_check.py >> ../results/backend_integration.log"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

import boto3
import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = {k: (v["value"] if isinstance(v, dict) and "value" in v else v)
       for k, v in json.loads((ROOT.parent / "results" / "infra_outputs.json").read_text()).items()}
REGION = OUT.get("region", "us-east-1")
ACCOUNT = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
idp = boto3.client("cognito-idp", region_name=REGION)
PORT = 8079


def log(*a):
    print(" ".join(str(x) for x in a).replace(ACCOUNT, "<acct>"), flush=True)


def main() -> int:
    log("\n=== Production-mode auth against the real Cognito pool " + "=" * 12)
    email = f"backend-check-{secrets.token_hex(3)}@example.com"
    password = "Aa1!" + secrets.token_urlsafe(18)
    pool, client = OUT["user_pool_id"], OUT["client_id"]
    origin = secrets.token_urlsafe(24)
    idp.admin_create_user(UserPoolId=pool, Username=email, MessageAction="SUPPRESS",
                          UserAttributes=[{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"}])
    server = None
    fails = []
    try:
        idp.admin_set_user_password(UserPoolId=pool, Username=email, Password=password, Permanent=True)
        auth = idp.initiate_auth(ClientId=client, AuthFlow="USER_PASSWORD_AUTH",
                                 AuthParameters={"USERNAME": email, "PASSWORD": password})["AuthenticationResult"]
        log("throwaway user created and signed in (USER_PASSWORD_AUTH); got ID and access tokens")
        env = {k: v for k, v in os.environ.items() if k != "DEV_AUTH"}
        env.update(ORIGIN_SECRET=origin, COGNITO_USER_POOL_ID=pool, COGNITO_CLIENT_ID=client, AWS_REGION=REGION,
                   TABLE_NAME=OUT["table"])
        server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:create_app", "--factory", "--port", str(PORT)],
                                  cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        base = f"http://127.0.0.1:{PORT}"
        for _ in range(100):
            try:
                httpx.get(f"{base}/healthz", timeout=1)
                break
            except httpx.HTTPError:
                time.sleep(0.2)
        o = {"X-Origin-Verify": origin}
        cases = [
            ("healthz, no origin header", "/healthz", {}, 200),
            ("ID token, no origin header", "/api/me", {"Authorization": f"Bearer {auth['IdToken']}"}, 403),
            ("ID token, wrong origin header", "/api/me", {"X-Origin-Verify": "nope", "Authorization": f"Bearer {auth['IdToken']}"}, 403),
            ("no token", "/api/me", o, 401),
            ("access token (token_use=access)", "/api/me", {**o, "Authorization": f"Bearer {auth['AccessToken']}"}, 401),
            ("tampered ID token", "/api/me", {**o, "Authorization": f"Bearer {auth['IdToken'][:-6]}AAAAAA"}, 401),
            ("ID token", "/api/me", {**o, "Authorization": f"Bearer {auth['IdToken']}"}, 200),
            ("ID token, list sandboxes (DynamoDB)", "/api/sandboxes", {**o, "Authorization": f"Bearer {auth['IdToken']}"}, 200),
        ]
        for label, path, headers, want in cases:
            r = httpx.get(base + path, headers=headers, timeout=20)
            ok = r.status_code == want
            body = r.json() if path != "/api/me" or r.status_code != 200 else {"sub": "<sub>", "email": r.json().get("email")}
            log(f"{'ok  ' if ok else 'FAIL'} {label:<38} -> {r.status_code} {json.dumps(body)[:90]}")
            if not ok:
                fails.append(label)
        if r.status_code == 200 and isinstance(r.json(), list):
            log("  sandboxes for the new user:", r.json())
    finally:
        if server:
            server.terminate()
            server.wait(10)
        idp.admin_delete_user(UserPoolId=pool, Username=email)
        log("throwaway user deleted")
    log("auth check failures:", fails or "none")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
