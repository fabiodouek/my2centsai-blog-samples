import json

import pytest

from app.config import Settings
from app.microvm import CA_ENV, MAX_PAYLOAD, build_run_payload, encode_payload, env_safe_name, run_params, ua_app_id

PH = {"anthropic": "sk-ant-api03-proxy-placeholder-" + "0" * 64 + "-AAAAAAAA",
      "github": "ghp_proxyplaceholder000000000000000000000", "demo": "demo-placeholder-token"}


def test_transparent_mode_has_no_proxy_settings():
    p = build_run_payload(PH, "us-east-1")
    env = p["env"]
    assert p["nameserver"] is None
    assert not any(k.lower().endswith("_proxy") for k in env)
    assert env["ANTHROPIC_API_KEY"] == PH["anthropic"]
    assert env["GH_TOKEN"] == PH["github"] and env["DEMO_TOKEN"] == PH["demo"]
    assert env["AWS_ACCESS_KEY_ID"] == "AKIAPLACEHOLDER00000" and env["AWS_SECRET_ACCESS_KEY"] == "placeholder-secret-not-real"
    assert env["AWS_REGION"] == "us-east-1" and env["AWS_EC2_METADATA_DISABLED"] == "true"
    assert env["AWS_REQUEST_CHECKSUM_CALCULATION"] == "when_required"   # uploads as one payload iron-proxy can re-sign
    for k, v in CA_ENV.items():
        assert env[k] == v


def test_size_limit():
    s = encode_payload(build_run_payload(PH, "us-east-1"))
    assert len(s.encode()) < MAX_PAYLOAD
    assert json.loads(s)["env"]["DEMO_TOKEN"] == "demo-placeholder-token"
    with pytest.raises(ValueError, match="max 4096"):
        encode_payload(build_run_payload(PH, "us-east-1", {"BIG": "x" * 5000}))


def test_payload_has_no_secret_references():
    s = encode_payload(build_run_payload(PH, "us-east-1"))
    assert "secretsmanager" not in s and "arn:" not in s


def test_run_params():
    st = Settings(image_arn="arn:aws:lambda:us-east-1:111:microvm-image:my2cents-ws-sandbox", connector_arn="arn:conn")
    p = run_params(st, idle_minutes=15, max_duration_s=28800, client_token="tok", payload="{}")
    assert p["ingressNetworkConnectors"] == [
        "arn:aws:lambda:us-east-1:aws:network-connector:aws-network-connector:HTTP_INGRESS",
        "arn:aws:lambda:us-east-1:aws:network-connector:aws-network-connector:SHELL_INGRESS"]
    assert p["egressNetworkConnectors"] == ["arn:conn"]
    assert p["idlePolicy"] == {"autoResumeEnabled": True, "maxIdleDurationSeconds": 900, "suspendedDurationSeconds": 28800}
    assert p["maximumDurationInSeconds"] == 28800 and p["clientToken"] == "tok"
    assert "executionRoleArn" not in p


def test_sandbox_name_in_env_is_shell_and_tmux_safe():
    assert env_safe_name("alpha one") == "alpha one"
    assert env_safe_name('x"; rm -rf / #$(id)`') == "x-- rm -rf - ---id--"
    assert env_safe_name("###") == "---" and env_safe_name("") == "sandbox"
    assert len(env_safe_name("n" * 100)) == 64
    p = build_run_payload(PH, "us-east-1", {"SANDBOX_NAME": env_safe_name("demo box")})
    assert p["env"]["SANDBOX_NAME"] == "demo box"


def test_ua_app_id_follows_sandbox_name():
    p = build_run_payload(PH, "us-east-1", {"SANDBOX_NAME": "sts demo@1"})
    assert p["env"]["AWS_SDK_UA_APP_ID"] == "ws-sts-demo-1"
    assert "AWS_SDK_UA_APP_ID" not in build_run_payload(PH, "us-east-1")["env"]
    assert len(ua_app_id("x" * 80)) == 50 and ua_app_id("$$$") == "ws-sandbox"
