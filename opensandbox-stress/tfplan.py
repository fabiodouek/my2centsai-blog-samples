#!/usr/bin/env python3
"""The two Terraform readers 01_metrics.sh uses (standard library only, run with python3).

  python3 tfplan.py vars <state.json
      state.json is `terraform show -json` of part 2's Terraform. Prints the -var values the
      cluster was built with, one KEY=value line each, for the shell to read (never echoed):
        region                    the `region` output
        public_access_cidr        the EKS endpoint's one allowed /32, from the cluster in state
        manage_ecr_scan_on_push   true when the registry scanning rule is in state
        container_insights        true when the CloudWatch add-on is in state now
      Exit 1 (and one line on stderr, with no value in it) when one of them cannot be read.

  python3 tfplan.py check on|off <plan.json
      plan.json is `terraform show -json <saved plan>`. Prints every planned change (address and
      actions, no values) and exits:
        0  every change is one the add-on brings or takes away (see ALLOWED) and there is one
        3  nothing to do: Container Insights is already on (or off)
        1  anything else is planned: the apply must not run
      "on" allows only creates of those resources, "off" only deletes. A data source that is
      read again is not a change and is allowed either way. A `moved` or `import` block (a change
      to state alone) is refused unless ALLOWED_STATE_CHANGES lists it.

Why so strict: a plain `terraform apply` of part 2's Terraform with the wrong -var values (a
different IP, the scan-on-push rule off) or after AWS published a newer add-on would change the
cluster itself. This apply may touch the CloudWatch add-on, its IAM role, the role's policy
attachment and its Pod Identity association, and nothing else.
"""

from __future__ import annotations

import json
import re
import sys

# The exact addresses part 2's Terraform gives the add-on's resources. Its Pod Identity association
# is a block inside the add-on resource (terraform/main.tf), not a resource of its own; a module
# version that split it out would be refused here until it is reviewed and added.
ALLOWED = (
    'module.eks.aws_eks_addon.this["amazon-cloudwatch-observability"]',
    'aws_iam_role.pod_identity["cloudwatch-agent"]',
    "aws_iam_role_policy_attachment.cloudwatch[0]",
)
# State-only changes a plan can carry: a `moved` block (previous_address) or an `import` block
# (change.importing). Applying the saved plan would rewrite or import state. None is expected;
# a reviewed one would be listed here as (address, kind).
ALLOWED_STATE_CHANGES: tuple[tuple[str, str], ...] = ()
ADDON = 'module.eks.aws_eks_addon.this["amazon-cloudwatch-observability"]'
SCAN_RULE = "aws_ecr_registry_scanning_configuration.scan_on_push[0]"


def allowed_address(address: str) -> bool:
    return address in ALLOWED


def review(plan: dict, want: str) -> tuple[int, list[str]]:
    """(exit code, lines to print) for a plan, as described in the module docstring."""
    if want not in ("on", "off"):
        raise ValueError("want must be 'on' or 'off'")
    action = "create" if want == "on" else "delete"
    lines, bad, good = [], 0, 0
    for change in plan.get("resource_changes", []):
        address = change.get("address", "?")
        actions = change.get("change", {}).get("actions", [])
        moved = change.get("previous_address") not in (None, address)
        imported = bool(change.get("change", {}).get("importing"))
        if moved or imported:
            kind = "moved" if moved else "import"
            if (address, kind) not in ALLOWED_STATE_CHANGES:
                origin = f" from {change['previous_address']}" if moved else ""
                lines.append(f"  FAIL  {address}: {kind}{origin} (a state change no reviewed list allows)")
                bad += 1
                continue
        if actions == ["no-op"]:
            continue
        if change.get("mode") == "data" and actions == ["read"]:
            lines.append(f"  ok    {address}: read (a data source, not a change)")
            continue
        if allowed_address(address) and actions == [action]:
            lines.append(f"  ok    {address}: {action}")
            good += 1
        else:
            why = "not one of the add-on's resources" if not allowed_address(address) else f"expected {action} only"
            lines.append(f"  FAIL  {address}: {'+'.join(actions) or '?'} ({why})")
            bad += 1
    for name, change in (plan.get("output_changes") or {}).items():
        actions = change.get("actions", [])
        if actions and actions != ["no-op"]:
            lines.append(f"  FAIL  output {name}: {'+'.join(actions)} (no output should change)")
            bad += 1
    if bad:
        lines.append(f"{bad} change(s) outside the CloudWatch add-on: refusing to apply")
        return 1, lines
    if not good:
        lines.append(f"no changes: Container Insights is already {want}")
        return 3, lines
    lines.append(f"{good} change(s), all of them the CloudWatch add-on's")
    return 0, lines


def _root_resources(state: dict) -> list[dict]:
    return state.get("values", {}).get("root_module", {}).get("resources", [])


def _module_resources(state: dict, address: str) -> list[dict]:
    for module in state.get("values", {}).get("root_module", {}).get("child_modules", []):
        if module.get("address") == address:
            return module.get("resources", [])
    return []


def built_with(state: dict) -> dict[str, str]:
    """The -var values the cluster was built with, from `terraform show -json` of its state.
    Raises ValueError (with no value in the message) when one cannot be read."""
    outputs = state.get("values", {}).get("outputs", {})
    region = (outputs.get("region") or {}).get("value")
    if not isinstance(region, str) or not re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d", region):
        raise ValueError("the state has no usable `region` output: run ../opensandbox-eks/01_cluster.sh first")
    clusters = [r for r in _module_resources(state, "module.eks") if r.get("type") == "aws_eks_cluster"]
    if len(clusters) != 1:
        raise ValueError(f"the state has {len(clusters)} EKS clusters in module.eks (expected 1)")
    vpc = (clusters[0].get("values", {}).get("vpc_config") or [{}])[0]
    cidrs = vpc.get("public_access_cidrs") or []
    if len(cidrs) != 1 or not re.fullmatch(r"(\d{1,3}\.){3}\d{1,3}/32", cidrs[0]):
        raise ValueError(f"the cluster in state allows {len(cidrs)} public CIDR(s), not one /32: "
                         "part 2's Terraform sets exactly one")
    addresses = {r.get("address") for r in _root_resources(state)}
    addons = {r.get("address") for r in _module_resources(state, "module.eks")}
    return {"region": region, "public_access_cidr": cidrs[0],
            "manage_ecr_scan_on_push": "true" if SCAN_RULE in addresses else "false",
            "container_insights": "true" if ADDON in addons else "false"}


def main(argv: list[str]) -> int:
    if len(argv) == 1 and argv[0] == "vars":
        try:
            values = built_with(json.load(sys.stdin))
        except (ValueError, json.JSONDecodeError) as error:
            print(f"tfplan.py vars: {error}", file=sys.stderr)
            return 1
        for key, value in values.items():
            print(f"{key}={value}")
        return 0
    if len(argv) == 2 and argv[0] == "check" and argv[1] in ("on", "off"):
        try:
            plan = json.load(sys.stdin)
        except json.JSONDecodeError as error:
            print(f"tfplan.py check: the plan is not JSON ({error})", file=sys.stderr)
            return 2
        code, lines = review(plan, argv[1])
        print("\n".join(lines))
        return code
    print("usage: python3 tfplan.py vars <state.json | python3 tfplan.py check on|off <plan.json", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
