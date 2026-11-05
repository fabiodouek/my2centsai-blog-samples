"""Settings from the environment. Production needs ORIGIN_SECRET, the Cognito pool/client and TABLE_NAME;
local development can run with DEV_AUTH=1 (a fixed fake user) and an in-memory store instead, but only
when ORIGIN_SECRET is unset so a deployed task can never fall into dev mode."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


class ConfigError(RuntimeError):
    pass


def _list(v: str | None) -> list[str]:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


def _json(name: str, v: str | None) -> dict:
    if not v:
        return {}
    try:
        d = json.loads(v)
    except ValueError as e:
        raise ConfigError(f"{name} is not valid JSON") from e
    if not isinstance(d, dict):
        raise ConfigError(f"{name} must be a JSON object")
    return d


# Names the proxy forwards (part 2's iron-proxy allowlist). Domain names only; override with PROXY_ALLOWLIST.
DEFAULT_ALLOWLIST = [
    "api.anthropic.com", "statsig.anthropic.com", "github.com", "api.github.com", "codeload.github.com",
    "*.githubusercontent.com", "httpbin.org", "sts.amazonaws.com", "sts.{region}.amazonaws.com",
    "s3.amazonaws.com", "*.s3.amazonaws.com", "s3.{region}.amazonaws.com", "*.s3.{region}.amazonaws.com",
    "ecs.{region}.amazonaws.com", "iam.amazonaws.com",
]

DEFAULT_CONTROL_KEYS = {"config": "iron-proxy.yaml", "scope": "aws-scope.json", "status": "status.json",
                        "credentials": "credentials.json", "default_config": "iron-proxy.default.yaml"}

MANAGED_CONNECTOR = "arn:aws:lambda:{region}:aws:network-connector:aws-network-connector:{name}"


@dataclass
class Settings:
    region: str = "us-east-1"
    table_name: str | None = None
    user_pool_id: str | None = None
    client_id: str | None = None
    # The medium (default) image; IMAGE_ARNS_JSON maps every size to its image (app/sizes.py)
    image_arn: str | None = None
    image_arns: dict = field(default_factory=dict)
    connector_arn: str | None = None
    # Transparent interception (part 2's two VPCs): no proxy settings in the sandbox; the only mode
    proxy_mode: str = "transparent"
    nlb_ips: list[str] = field(default_factory=list)
    placeholders: dict = field(default_factory=dict)
    secret_arns: dict = field(default_factory=dict)
    proxy_log_group: str | None = None
    dns_log_group: str | None = None
    origin_secret: str | None = None
    task_role_arn: str | None = None
    dev_auth: bool = False
    # Fallback for the MicroVM memory quota when Service Quotas can't be read (app/quota.py reads the real one)
    quota_gb: int = 8
    quota_service_code: str = "lambda"
    quota_code: str = "L-CD1C0CC4"     # "Max allocated ARM_64 MicroVM memory", GB
    vm_memory_gb: int = 2
    max_duration_s: int = 28800
    proxy_allowlist: list[str] = field(default_factory=list)
    # Part 2's sandbox subnets (the egress connector's ENIs): DNS queries from these came from a sandbox.
    sandbox_cidrs: list[str] = field(default_factory=list)
    cors_origins: list[str] = field(default_factory=list)
    terminal_keepalive_s: float = 20.0
    proxy_ecs_cluster: str = "my2cents-ip"
    proxy_ecs_service: str = "iron-proxy"
    # Proxy control plane (part 2 applied with control_plane=true). Off when CONTROL_BUCKET is unset.
    control_bucket: str | None = None
    control_keys: dict = field(default_factory=lambda: dict(DEFAULT_CONTROL_KEYS))
    # How often part 2's control sidecar checks the bucket (its control_poll_s). status.json reports the value it
    # runs with; this is the fallback until it does.
    control_poll_s: float = 30.0
    # Part 2's sandbox roles: {"view-only": {"arn", "description", "default"}, ...}
    sandbox_roles: dict = field(default_factory=dict)
    scope_baseline: dict = field(default_factory=dict)
    user_secret_prefix: str = "my2cents-ip/user/"
    changes_table: str | None = None
    admin_group: str = "admins"
    # The AWS access catalog: AWS's Service Authorization Reference, cached in memory and on local disk
    service_reference_url: str = "https://servicereference.us-east-1.amazonaws.com/"
    catalog_ttl_s: float = 3600.0
    catalog_cache_dir: str | None = "/tmp/aws-catalog"

    @property
    def sandbox_role_arns(self) -> list[str]:
        return [r["arn"] for r in self.sandbox_roles.values() if isinstance(r, dict) and r.get("arn")]

    @property
    def size_images(self) -> dict[str, str]:
        """size -> image ARN for the sizes this deployment has an image ARN for (medium falls back to IMAGE_ARN)."""
        out = {k: v for k, v in self.image_arns.items() if v}
        if self.image_arn and "medium" not in out:
            out["medium"] = self.image_arn
        return out

    @property
    def control_plane(self) -> bool:
        return bool(self.control_bucket)

    @property
    def http_ingress(self) -> str:
        return MANAGED_CONNECTOR.format(region=self.region, name="HTTP_INGRESS")

    @property
    def shell_ingress(self) -> str:
        return MANAGED_CONNECTOR.format(region=self.region, name="SHELL_INGRESS")

    @property
    def issuer(self) -> str:
        return f"https://cognito-idp.{self.region}.amazonaws.com/{self.user_pool_id}"

    @property
    def jwks_url(self) -> str:
        return f"{self.issuer}/.well-known/jwks.json"

    def validate(self) -> None:
        if self.dev_auth and self.origin_secret:
            raise ConfigError("DEV_AUTH=1 is refused when ORIGIN_SECRET is set (that means a deployed task)")
        if not self.dev_auth:
            missing = [n for n, v in (("ORIGIN_SECRET", self.origin_secret), ("COGNITO_USER_POOL_ID", self.user_pool_id),
                                      ("COGNITO_CLIENT_ID", self.client_id), ("TABLE_NAME", self.table_name)) if not v]
            if missing:
                raise ConfigError(f"missing required settings: {', '.join(missing)} (set DEV_AUTH=1 for local development)")
        if self.proxy_mode != "transparent":
            raise ConfigError("PROXY_MODE must be 'transparent' (part 2's two-VPC design; no proxy settings in sandboxes)")
        if not 1 <= self.max_duration_s <= 28800:
            raise ConfigError("MAX_DURATION_S must be 1..28800")
        from .sizes import SIZES
        unknown = sorted(set(self.image_arns) - set(SIZES))
        if unknown:
            raise ConfigError(f"IMAGE_ARNS_JSON has unknown sizes: {', '.join(unknown)} (known: {', '.join(SIZES)})")
        if self.control_bucket and not (self.sandbox_roles and self.scope_baseline):
            raise ConfigError("CONTROL_BUCKET needs ROLES_JSON and SCOPE_BASELINE_JSON (part 2's outputs)")
        if not self.user_secret_prefix.endswith("/"):
            raise ConfigError("USER_SECRET_PREFIX must end with /")


def load(env: dict | None = None) -> Settings:
    e = os.environ if env is None else env
    region = e.get("AWS_REGION") or e.get("AWS_DEFAULT_REGION") or "us-east-1"
    allow = _list(e.get("PROXY_ALLOWLIST")) or [d.format(region=region) for d in DEFAULT_ALLOWLIST]
    s = Settings(
        region=region,
        table_name=e.get("TABLE_NAME") or None,
        user_pool_id=e.get("COGNITO_USER_POOL_ID") or None,
        client_id=e.get("COGNITO_CLIENT_ID") or None,
        image_arn=e.get("IMAGE_ARN") or None,
        image_arns=_json("IMAGE_ARNS_JSON", e.get("IMAGE_ARNS_JSON")),
        connector_arn=e.get("CONNECTOR_ARN") or None,
        proxy_mode=(e.get("PROXY_MODE") or "transparent").strip().lower(),
        nlb_ips=_list(e.get("NLB_IPS")),
        placeholders=_json("PLACEHOLDERS_JSON", e.get("PLACEHOLDERS_JSON")),
        secret_arns=_json("SECRET_ARNS_JSON", e.get("SECRET_ARNS_JSON")),
        proxy_log_group=e.get("PROXY_LOG_GROUP_ARN") or None,
        dns_log_group=e.get("DNS_LOG_GROUP_ARN") or None,
        origin_secret=e.get("ORIGIN_SECRET") or None,
        task_role_arn=e.get("TASK_ROLE_ARN") or None,
        dev_auth=e.get("DEV_AUTH") == "1",
        quota_gb=int(e.get("QUOTA_GB") or 8),
        quota_service_code=e.get("QUOTA_SERVICE_CODE") or "lambda",
        quota_code=e.get("QUOTA_CODE") or "L-CD1C0CC4",
        vm_memory_gb=int(e.get("VM_MEMORY_GB") or 2),
        max_duration_s=int(e.get("MAX_DURATION_S") or 28800),
        proxy_allowlist=allow,
        sandbox_cidrs=_list(e.get("SANDBOX_CIDRS")),
        cors_origins=_list(e.get("CORS_ORIGINS")),
        terminal_keepalive_s=float(e.get("TERMINAL_KEEPALIVE_S") or 20),
        proxy_ecs_cluster=e.get("PROXY_ECS_CLUSTER") or "my2cents-ip",
        proxy_ecs_service=e.get("PROXY_ECS_SERVICE") or "iron-proxy",
        control_bucket=e.get("CONTROL_BUCKET") or None,
        control_keys={**DEFAULT_CONTROL_KEYS, **_json("CONTROL_KEYS_JSON", e.get("CONTROL_KEYS_JSON"))},
        control_poll_s=float(e.get("CONTROL_POLL_S") or 30),
        sandbox_roles=_json("ROLES_JSON", e.get("ROLES_JSON")),
        scope_baseline=_json("SCOPE_BASELINE_JSON", e.get("SCOPE_BASELINE_JSON")),
        user_secret_prefix=e.get("USER_SECRET_PREFIX") or "my2cents-ip/user/",
        changes_table=e.get("CHANGES_TABLE") or None,
        admin_group=e.get("ADMIN_GROUP") or "admins",
        service_reference_url=e.get("SERVICE_REFERENCE_URL") or "https://servicereference.us-east-1.amazonaws.com/",
        catalog_ttl_s=float(e.get("CATALOG_TTL_S") or 3600),
        catalog_cache_dir=e.get("CATALOG_CACHE_DIR", "/tmp/aws-catalog") or None,
    )
    s.validate()
    return s
