"""tfplan.py: the plan filter 01_metrics.sh relies on (and 99_cleanup.py through it), and the reader
of the -var values the cluster was built with. Plans and state in the shape of `terraform show -json`."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

import tfplan

HERE = Path(__file__).resolve().parent.parent
ADDON = 'module.eks.aws_eks_addon.this["amazon-cloudwatch-observability"]'
ROLE = 'aws_iam_role.pod_identity["cloudwatch-agent"]'
ATTACH = "aws_iam_role_policy_attachment.cloudwatch[0]"
TRUST = 'data.aws_iam_policy_document.pod_identity_trust["cloudwatch-agent"]'


def change(address: str, *actions: str, mode: str = "managed") -> dict:
    return {"address": address, "mode": mode, "change": {"actions": list(actions)}}


def plan(*changes: dict, outputs: dict | None = None) -> dict:
    return {"format_version": "1.2", "resource_changes": list(changes), "output_changes": outputs or {}}


ON = plan(change(ADDON, "create"), change(ROLE, "create"), change(ATTACH, "create"), change(TRUST, "read", mode="data"),
          change("module.eks.aws_eks_cluster.this[0]", "no-op"),
          outputs={"region": {"actions": ["no-op"]}})
OFF = plan(change(ADDON, "delete"), change(ROLE, "delete"), change(ATTACH, "delete"))


def test_turning_on_passes():
    code, lines = tfplan.review(ON, "on")
    assert code == 0
    assert lines[-1] == "3 change(s), all of them the CloudWatch add-on's"
    assert any(TRUST in line and "data source" in line for line in lines)


def test_turning_off_passes():
    assert tfplan.review(OFF, "off")[0] == 0


def test_direction_matters():
    code, lines = tfplan.review(ON, "off")
    assert code == 1 and any("expected delete only" in line for line in lines)


def test_nothing_to_do():
    code, lines = tfplan.review(plan(change(ADDON, "no-op")), "on")
    assert code == 3 and lines[-1] == "no changes: Container Insights is already on"


@pytest.mark.parametrize("bad", [
    change('module.eks.module.eks_managed_node_group["nodes"].aws_eks_node_group.this[0]', "update"),
    change("module.eks.aws_eks_cluster.this[0]", "update"),
    change('module.eks.aws_eks_addon.this["coredns"]', "update"),
    change("aws_ecr_registry_scanning_configuration.scan_on_push[0]", "delete"),
    change(ADDON, "delete", "create"),
])
def test_anything_else_refuses(bad):
    code, lines = tfplan.review(plan(change(ADDON, "create"), bad), "on")
    assert code == 1 and lines[-1].endswith("refusing to apply")


def test_an_output_change_refuses():
    code, _ = tfplan.review(plan(change(ADDON, "create"), outputs={"vpc_id": {"actions": ["update"]}}), "on")
    assert code == 1


def test_a_separate_pod_identity_association_is_refused_until_reviewed():
    assoc = 'module.eks.aws_eks_pod_identity_association.this["amazon-cloudwatch-observability"]'
    assert tfplan.review(plan(change(ADDON, "create"), change(assoc, "create")), "on")[0] == 1


def test_a_moved_block_is_refused():
    moved = {"address": "module.eks.aws_eks_cluster.main[0]", "previous_address": "module.eks.aws_eks_cluster.this[0]",
             "mode": "managed", "change": {"actions": ["no-op"]}}
    code, lines = tfplan.review(plan(change(ADDON, "create"), moved), "on")
    assert code == 1 and any("moved from module.eks.aws_eks_cluster.this[0]" in line for line in lines)


def test_an_import_block_is_refused():
    imported = {"address": "aws_iam_role.pod_identity[\"other\"]", "mode": "managed",
                "change": {"actions": ["no-op"], "importing": {"id": "osb-eks-other"}}}
    code, lines = tfplan.review(plan(change(ADDON, "create"), imported), "on")
    assert code == 1 and any(": import" in line for line in lines)


def test_unchanged_previous_address_is_not_a_move():
    same = {"address": ADDON, "previous_address": ADDON, "mode": "managed", "change": {"actions": ["create"]}}
    assert tfplan.review(plan(same), "on")[0] == 0


def state(cidrs=("203.0.113.7/32",), scan=True, addon=False, region="us-east-1") -> dict:
    eks = [{"address": "module.eks.aws_eks_cluster.this[0]", "type": "aws_eks_cluster",
            "values": {"vpc_config": [{"public_access_cidrs": list(cidrs)}]}}]
    if addon:
        eks.append({"address": ADDON, "type": "aws_eks_addon", "values": {}})
    root = [{"address": "aws_vpc_endpoint.s3", "type": "aws_vpc_endpoint"}]
    if scan:
        root.append({"address": "aws_ecr_registry_scanning_configuration.scan_on_push[0]"})
    return {"values": {"outputs": {"region": {"value": region}},
                       "root_module": {"resources": root,
                                       "child_modules": [{"address": "module.eks", "resources": eks}]}}}


def test_built_with():
    assert tfplan.built_with(state()) == {"region": "us-east-1", "public_access_cidr": "203.0.113.7/32",
                                          "manage_ecr_scan_on_push": "true", "container_insights": "false"}
    assert tfplan.built_with(state(scan=False, addon=True))["manage_ecr_scan_on_push"] == "false"
    assert tfplan.built_with(state(addon=True))["container_insights"] == "true"


@pytest.mark.parametrize("bad", [state(cidrs=()), state(cidrs=("0.0.0.0/0",)),
                                 state(cidrs=("203.0.113.7/32", "198.51.100.1/32")), state(region="nowhere")])
def test_built_with_refuses_and_never_names_the_value(bad):
    with pytest.raises(ValueError) as error:
        tfplan.built_with(bad)
    assert "203.0.113" not in str(error.value) and "0.0.0.0" not in str(error.value)


def run(*args: str, stdin: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(HERE / "tfplan.py"), *args], input=stdin, capture_output=True, text=True)


def test_cli_vars_output_is_what_01_metrics_reads():
    done = run("vars", stdin=json.dumps(state()))
    assert done.returncode == 0
    pairs = dict(line.split("=", 1) for line in done.stdout.splitlines())
    assert set(pairs) == {"region", "public_access_cidr", "manage_ecr_scan_on_push", "container_insights"}


def test_cli_vars_failure_is_one_line_without_a_value():
    done = run("vars", stdin=json.dumps(state(cidrs=("203.0.113.7/32", "198.51.100.1/32"))))
    assert done.returncode == 1 and done.stdout == "" and "203.0.113" not in done.stderr


def test_cli_check_exit_codes():
    assert run("check", "on", stdin=json.dumps(ON)).returncode == 0
    assert run("check", "on", stdin=json.dumps(plan())).returncode == 3
    assert run("check", "off", stdin=json.dumps(ON)).returncode == 1
    assert run("check", "on", stdin="not json").returncode == 2
    assert run("bogus", stdin="").returncode == 2
