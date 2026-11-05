"""Sandbox sizes. A MicroVM's memory comes from its image (`resources[].minimumMemoryInMiB`; vCPU is memory / 2 GB,
with bursts up to 4x), so each size is its own image built from the same code (scripts/build_sandbox_image.py).
Medium is the original image and the default."""

from __future__ import annotations

from . import pricing

SIZES: dict[str, dict] = {
    "small": {"label": "Small", "memory_mib": 1024, "vcpu": 0.5},
    "medium": {"label": "Medium", "memory_mib": 2048, "vcpu": 1.0},
    "large": {"label": "Large", "memory_mib": 4096, "vcpu": 2.0},
    "xlarge": {"label": "X-Large", "memory_mib": 8192, "vcpu": 4.0},
}
DEFAULT_SIZE = "medium"
# Image names (scripts/mvm.py has the same map): medium keeps the original name
IMAGE_SUFFIX = {"small": "-1g", "medium": "", "large": "-4g", "xlarge": "-8g"}
BURST = 4  # a MicroVM bursts to 4x its baseline vCPU; inside, nproc and free show 4x the size


def gb(size: str) -> float:
    return SIZES[size]["memory_mib"] / 1024


def describe(size: str, quota_gb: float) -> dict:
    d = SIZES[size]
    out = {"id": size, "label": d["label"], "memory_gb": gb(size), "memory_mib": d["memory_mib"], "vcpu": d["vcpu"],
           "usd_per_hour": pricing.microvm_hourly(d["vcpu"], gb(size)), "note": None}
    # A hint, never a block: the quota is our reading of Service Quotas and AWS decides (RunMicrovm is always called)
    if gb(size) > quota_gb:
        out["note"] = (f"Bigger than this account's {quota_gb:g} GB MicroVM memory quota as we read it: AWS will refuse "
                       "it unless the quota has been raised.")
    elif gb(size) == quota_gb:
        out["note"] = (f"Needs all {quota_gb:g} GB of this account's MicroVM memory quota: AWS will refuse it if anything "
                       "else is running or suspended.")
    return out


def size_of_arn(image_arns: dict[str, str], arn: str | None) -> str | None:
    """The size whose image this is (versions and qualifiers ignored), or None for an image that isn't ours."""
    if not arn:
        return None
    base = arn.split(":microvm-image:", 1)[-1].split(":", 1)[0].split("/", 1)[0]
    for size, a in image_arns.items():
        if a and a.split(":microvm-image:", 1)[-1] == base:
            return size
    return None
