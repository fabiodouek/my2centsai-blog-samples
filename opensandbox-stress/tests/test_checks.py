"""checks.py: the preconditions, the baseline check, why the cluster refused a sandbox, what the
scheduler has reserved, and which snapshot images are ours. Recorded-shape JSON, no cluster."""

import pytest

from checks import (
    BASELINE,
    allocation,
    counted,
    cpu_milli,
    deletable_images,
    delivery_size,
    dropped_runtimes,
    egress_rebuilt,
    helm_problems,
    helm_state,
    hog_verdict,
    leftover_problems,
    mem_bytes,
    node_problems,
    other_pods,
    pod_requests,
    precondition_problems,
    put_back,
    refusal,
    scheduling_reasons,
    server_problems,
)

SHOW = {"runtime": "runc", "egress_image": "base", "egress_mode": "dns+nft", "ingress": "gateway",
        "gateway_values": True, "revision": 15}


def node(name: str, ready: bool = True, cordoned: bool = False, pressure: str | None = None,
         pods: str = "58", cpu: str = "7910m", memory: str = "31296872Ki") -> dict:
    conditions = [{"type": "Ready", "status": "True" if ready else "False"}]
    conditions += [{"type": kind, "status": "True" if kind == pressure else "False"}
                   for kind in ("MemoryPressure", "DiskPressure", "PIDPressure")]
    return {"metadata": {"name": name}, "spec": {"unschedulable": True} if cordoned else {},
            "status": {"conditions": conditions, "allocatable": {"pods": pods, "cpu": cpu, "memory": memory}}}


NODES = {"items": [node("ip-10-0-0-2.ec2.internal"), node("ip-10-0-0-1.ec2.internal")]}


# --- the server and the nodes ------------------------------------------------------------------------

def test_baseline_holds():
    assert server_problems(SHOW, BASELINE) == []


def test_baseline_differences_are_named():
    state = {**SHOW, "runtime": "kata-qemu", "egress_image": "opensandbox-eks/egress-sigv4@sha256:abc"}
    found = server_problems(state, BASELINE)
    assert any("runtime is 'kata-qemu'" in f for f in found)
    assert any("egress_image" in f for f in found)


def test_missing_gateway_values_file_is_a_problem():
    assert any("server-gateway.yaml" in f for f in server_problems({**SHOW, "gateway_values": False}, BASELINE))


def test_nodes_ready():
    assert node_problems(NODES) == []


def test_node_problems_never_print_the_name():
    nodes = {"items": [node("ip-10-0-0-1.ec2.internal", ready=False), node("ip-10-0-0-2.ec2.internal", cordoned=True,
                                                                           pressure="MemoryPressure")]}
    found = node_problems(nodes)
    assert found[0].startswith("1 of 2 nodes Ready")
    assert "node-a is not Ready" in found and "node-b is cordoned" in found and "node-b reports MemoryPressure" in found
    assert not any("ip-" in f for f in found)


def test_preconditions_quiet_cluster():
    assert precondition_problems(server_state=SHOW, want_server=BASELINE, nodes=NODES, sandboxes=[],
                                 batchsandboxes=0, pools=[]) == []


def test_preconditions_refuse_any_sandbox_and_any_pool():
    sandboxes = [{"metadata": {"example": "opensandbox-stress"}}, {"metadata": {"example": "opensandbox-egress"}},
                 {"metadata": {}}]
    found = precondition_problems(server_state=SHOW, want_server=BASELINE, nodes=NODES, sandboxes=sandboxes,
                                  batchsandboxes=3, pools=["osb-pool"])
    assert "1 with example=opensandbox-stress" in found[0] and "1 with example=opensandbox-egress" in found[0]
    assert "the owning part's cleanup script" in found[0]
    assert any("osb-pool" in f for f in found)


def test_preconditions_point_at_our_cleanup_for_our_leftovers():
    found = precondition_problems(server_state=SHOW, want_server=BASELINE, nodes=NODES,
                                  sandboxes=[{"metadata": {"example": "opensandbox-stress"}}], batchsandboxes=1, pools=[])
    assert "99_cleanup.py" in found[0] and len(found) == 1


def test_preconditions_unlisted_batchsandboxes():
    found = precondition_problems(server_state=SHOW, want_server=BASELINE, nodes=NODES, sandboxes=[],
                                  batchsandboxes=2, pools=[])
    assert found == ["2 BatchSandbox object(s) exist in the sandbox namespace that the server does not list"]


# --- why the cluster refused ---------------------------------------------------------------------------

def test_scheduling_reasons():
    msg = ("0/2 nodes are available: 1 Insufficient cpu, 1 Too many pods. preemption: 0/2 nodes are available: "
           "2 No preemption victims found for incoming pod.")
    assert scheduling_reasons(msg) == {"cpu": 1, "pods": 1}
    assert scheduling_reasons("0/2 nodes are available: 2 Insufficient memory.") == {"memory": 2}
    assert scheduling_reasons("0/2 nodes are available: 2 node(s) were unschedulable.") == {"node(s) were unschedulable": 2}


def pending(name: str, message: str) -> dict:
    return {"metadata": {"name": name}, "spec": {},
            "status": {"phase": "Pending", "conditions": [{"type": "PodScheduled", "status": "False",
                                                          "reason": "Unschedulable", "message": message}]}}


def test_refusal_from_a_pending_pod():
    pods = {"items": [pending("abc-0", "0/2 nodes are available: 2 Too many pods. preemption: 0/2 ...")]}
    limit, detail = refusal(pods, {"items": []})
    assert limit == "pods" and detail == "0/2 nodes are available: 2 Too many pods."


def test_refusal_picks_the_reason_on_most_nodes():
    pods = {"items": [pending("abc-0", "0/2 nodes are available: 1 Insufficient memory, 2 Insufficient cpu.")]}
    assert refusal(pods, {"items": []})[0] == "cpu"


def test_refusal_from_events_when_the_pod_is_gone():
    events = {"items": [{"reason": "FailedScheduling", "involvedObject": {"name": "abc-0"},
                         "message": "0/2 nodes are available: 2 Insufficient cpu."}]}
    assert refusal({"items": []}, events) == ("cpu", "0/2 nodes are available: 2 Insufficient cpu.")


def test_refusal_ip_exhaustion():
    events = {"items": [{"reason": "FailedCreatePodSandBox", "involvedObject": {"name": "abc-0"},
                         "message": "plugin type=aws-cni failed (add): add cmd: failed to assign an IP address to container"}]}
    assert refusal({"items": []}, events)[0] == "ip"


def test_refusal_timeout_for_a_placed_pod():
    pods = {"items": [{"metadata": {"name": "abc-0"}, "spec": {"nodeName": "n"},
                       "status": {"phase": "Pending", "conditions": [{"type": "PodScheduled", "status": "True"}],
                                  "containerStatuses": [{"state": {"waiting": {"reason": "ContainerCreating"}}}]}}]}
    assert refusal(pods, {"items": []}) == ("timeout", "placed on a node but not ready in time (ContainerCreating)")


def test_refusal_only_considers_the_given_ids():
    pods = {"items": [pending("other-0", "0/2 nodes are available: 2 Too many pods.")]}
    assert refusal(pods, {"items": []}, sandbox_ids=["abc"]) == ("unknown", "")


# --- what the scheduler has reserved ---------------------------------------------------------------------

def test_quantities():
    assert cpu_milli("250m") == 250 and cpu_milli("2") == 2000 and cpu_milli("0.5") == 500 and cpu_milli(None) == 0
    assert mem_bytes("128Mi") == 128 * 2**20 and mem_bytes("1Gi") == 2**30 and mem_bytes("500M") == 500e6
    assert mem_bytes("31296872Ki") == 31296872 * 1024 and mem_bytes("1024") == 1024


def test_pod_requests_init_containers_and_kata_overhead():
    pod = {"spec": {"containers": [{"resources": {"requests": {"cpu": "100m", "memory": "128Mi"}}},
                                   {"name": "egress"}],
                    "initContainers": [{"resources": {"requests": {"cpu": "500m", "memory": "64Mi"}}}],
                    "overhead": {"cpu": "250m", "memory": "160Mi"}}}
    cpu, mem = pod_requests(pod)
    assert cpu == 750                      # the init container's 500m is larger than the containers' 100m
    assert mem == (128 + 160) * 2**20      # the containers' 128Mi is larger than the init's 64Mi


def test_allocation_per_node():
    pods = {"items": [
        {"spec": {"nodeName": "ip-10-0-0-1.ec2.internal",
                  "containers": [{"resources": {"requests": {"cpu": "791m", "memory": "1Gi"}}}]}, "status": {"phase": "Running"}},
        {"spec": {"nodeName": "ip-10-0-0-1.ec2.internal", "containers": [{}]}, "status": {"phase": "Succeeded"}},
        {"spec": {"containers": [{}]}, "status": {"phase": "Pending"}},
    ]}
    out = allocation(pods, {"items": [node("ip-10-0-0-1.ec2.internal", cpu="7910m", memory="10Gi"),
                                      node("ip-10-0-0-2.ec2.internal")]})
    assert out["node-a"] == {"pods": 1, "pods_max": 58, "cpu_pct": 10.0, "mem_pct": 10.0}
    assert out["node-b"]["pods"] == 0


# --- snapshot images ------------------------------------------------------------------------------------

OURS_GONE = "11111111-1111-4111-8111-111111111111"
OURS_PAUSED = "22222222-2222-4222-8222-222222222222"
OTHER = "33333333-3333-4333-8333-333333333333"


def image(tag, digest):
    return {"imageDigest": f"sha256:{digest * 64}", **({"imageTag": tag} if tag else {})}


@pytest.mark.parametrize("ours,existing,expected", [
    ({OURS_GONE, OURS_PAUSED}, {OURS_PAUSED}, ([f"{OURS_GONE}-egress-snap-gen2", f"{OURS_GONE}-sandbox-snap-gen2"], 1, 2)),
    (set(), set(), ([], 0, 5)),
    ({OURS_PAUSED}, {OURS_PAUSED}, ([], 1, 4)),
])
def test_deletable_images(ours, existing, expected):
    images = [image(f"{OURS_GONE}-sandbox-snap-gen2", "a"), image(f"{OURS_GONE}-egress-snap-gen2", "b"),
              image(f"{OURS_PAUSED}-sandbox-snap-gen2", "c"), image(f"{OTHER}-sandbox-snap-gen2", "d"), image(None, "e")]
    delete, kept, others = deletable_images(images, ours, existing)
    assert (sorted(d["imageTag"] for d in delete), kept, others) == expected


# --- how many warm sandboxes the delivery test can hand out ---------------------------------------------

def test_delivery_size_on_these_nodes():
    n, why = delivery_size([58, 58], placed=25, margin=4, wanted=100)
    assert n == 87
    assert why == ("58 pod slots per node x 2 nodes = 116, minus 25 pods already placed (system pods), "
                   "minus 4 headroom = 87: 87 instead of 100")


def test_delivery_size_when_everything_fits():
    n, why = delivery_size([110, 110], placed=20, margin=4, wanted=100)
    assert n == 100 and why.endswith(": 100 fit")


def test_delivery_size_uneven_nodes_and_no_room():
    n, why = delivery_size([58, 30], placed=90, margin=4, wanted=100)
    assert n == 0 and why.startswith("58 + 30 = 88 pod slots")



# --- the Helm release and putting the baseline back (C1: a failed upgrade) ------------------------------

def history(*entries):
    return [{"revision": r, "status": st} for r, st in entries]


class FakeServer:
    """A server release driven through put_back(): --baseline makes a new deployed revision."""

    def __init__(self, revisions, show_state=None, baseline_fails=False, pending_polls=0):
        self.revisions = list(revisions)
        self.state = dict(show_state or SHOW)
        self.baseline_fails = baseline_fails
        self.pending_polls = pending_polls
        self.baselines = 0
        self.waits = 0

    def history(self):
        if self.pending_polls:
            self.pending_polls -= 1
            if not self.pending_polls:   # the pending upgrade ends, failed
                self.revisions[-1] = {**self.revisions[-1], "status": "failed"}
        return list(self.revisions)

    def show(self):
        return dict(self.state)

    def run_baseline(self):
        self.baselines += 1
        if self.baseline_fails:
            raise RuntimeError("server_config.py --baseline failed (exit 1)")
        latest = max(int(r["revision"]) for r in self.revisions)
        self.revisions = [{**r, "status": "superseded"} if r["status"] == "deployed" else r for r in self.revisions]
        self.revisions.append({"revision": latest + 1, "status": "deployed"})
        self.state = dict(SHOW)

    def wait(self, _seconds):
        self.waits += 1


def test_helm_state_and_problems():
    assert helm_state(history((14, "superseded"), (15, "deployed"))) == {"deployed": 15, "latest": 15,
                                                                         "latest_status": "deployed"}
    failed = helm_state(history((15, "deployed"), (16, "failed")))
    assert failed == {"deployed": 15, "latest": 16, "latest_status": "failed"}
    assert helm_problems(failed) == ["the server's Helm release: latest revision 16 is failed, the last deployed one is 15"]
    assert helm_problems(helm_state([])) == ["the server's Helm release has no history"]


def test_a_failed_upgrade_is_put_back_although_show_says_baseline():
    """--runtime kata-qemu failed: revision 16 is failed, --show still reports 15 (runc)."""
    server = FakeServer(history((15, "deployed"), (16, "failed")))
    problems = put_back(touched=True, run_baseline=server.run_baseline, show=server.show, history=server.history,
                        wait=server.wait)
    assert server.baselines == 1 and problems == []
    assert helm_state(server.history())["latest_status"] == "deployed"


def test_an_untouched_run_with_a_failed_revision_is_put_back_too():
    server = FakeServer(history((15, "deployed"), (16, "failed")))
    assert put_back(touched=False, run_baseline=server.run_baseline, show=server.show, history=server.history,
                    wait=server.wait) == []
    assert server.baselines == 1


def test_a_settled_baseline_is_left_alone():
    server = FakeServer(history((15, "deployed")))
    assert put_back(touched=False, run_baseline=server.run_baseline, show=server.show, history=server.history,
                    wait=server.wait) == []
    assert server.baselines == 0


def test_a_pending_upgrade_is_waited_for_first():
    server = FakeServer(history((15, "deployed"), (16, "pending-upgrade")), pending_polls=3)
    assert put_back(touched=True, run_baseline=server.run_baseline, show=server.show, history=server.history,
                    wait=server.wait, poll_s=5) == []
    assert server.waits == 2 and server.baselines == 1


def test_a_release_that_stays_pending_is_reported_not_forced():
    server = FakeServer(history((15, "deployed"), (16, "pending-upgrade")), pending_polls=10**6)
    problems = put_back(touched=True, run_baseline=server.run_baseline, show=server.show, history=server.history,
                        wait=server.wait, settle_s=20, poll_s=5)
    assert server.baselines == 0 and "still pending-upgrade" in problems[0]


def test_a_failing_baseline_is_reported_with_the_read_back():
    server = FakeServer(history((15, "deployed"), (16, "failed")), show_state={**SHOW, "runtime": "kata-qemu"},
                        baseline_fails=True)
    problems = put_back(touched=True, run_baseline=server.run_baseline, show=server.show, history=server.history,
                        wait=server.wait)
    assert problems[0].startswith("putting the baseline back failed: RuntimeError")
    assert any("latest revision 16 is failed" in p for p in problems)
    assert any("runtime is 'kata-qemu'" in p for p in problems)


def test_an_interruption_during_the_baseline_is_reported():
    server = FakeServer(history((15, "deployed")))

    def interrupted():
        raise KeyboardInterrupt

    problems = put_back(touched=True, run_baseline=interrupted, show=server.show, history=server.history,
                        wait=server.wait)
    assert problems == ["putting the baseline back failed: KeyboardInterrupt: "]


# --- leftovers, other pods, counted runs, rebuild notice --------------------------------------------------

def test_leftover_problems():
    objects = [{"kind": "Deployment", "metadata": {"name": "echo-stress"}},
               {"kind": "Pod", "metadata": {"name": "osb-stress-vmm-node-a"}},
               {"kind": "Pod", "metadata": {"name": "osb-stress-client", "deletionTimestamp": "2026-11-01T00:00:00Z"}}]
    [problem] = leftover_problems(objects)
    assert "Deployment/echo-stress" in problem and "Pod/osb-stress-vmm-node-a" in problem
    assert "osb-stress-client" not in problem and "99_cleanup.py" in problem
    assert leftover_problems([]) == []


def test_other_pods():
    pods = {"items": [
        {"metadata": {"name": "echo-85bcfb9b76-8xpws", "labels": {"app": "echo"}}, "status": {"phase": "Running"}},
        {"metadata": {"name": "sb-0", "labels": {"opensandbox.io/id": "sb"}}, "status": {"phase": "Running"}},
        {"metadata": {"name": "ours", "labels": {"example": "opensandbox-stress"}}, "status": {"phase": "Running"}},
        {"metadata": {"name": "done", "labels": {}}, "status": {"phase": "Succeeded"}}]}
    assert other_pods(pods) == ["echo-85bcfb9b76-8xpws"]


def test_counted_leaves_failed_runs_out_or_flags():
    runs = [{"run": 1, "failed": 0}, {"run": 2, "failed": 3}, {"run": 3}]
    assert counted(runs) == ([{"run": 1, "failed": 0}, {"run": 3}], False)
    assert counted([{"run": 1, "failed": 1}]) == ([{"run": 1, "failed": 1}], True)
    assert counted([]) == ([], False)


def test_egress_rebuilt():
    assert egress_rebuilt("  ok    already in ECR (same Dockerfile and addon): x, not rebuilt") is False
    assert egress_rebuilt("  ok    pushed opensandbox-eks/egress-sigv4:release-1.2.0") is True
    assert egress_rebuilt("") is None


# --- 07_noisy.py: hogs and its results ------------------------------------------------------------

def test_a_spinning_cpu_hog_is_running_under_either_runtime():
    # Under Kata the kubelet reports 0 for the pod; the verdict reads the sandbox's own count.
    assert hog_verdict("cpu", {"procs": 3, "cpu_m": 1450.0, "newest_write_s": None}) == (True, "")


@pytest.mark.parametrize("probe, needle", [
    (None, "did not answer"),
    ({}, "did not answer"),
    ({"procs": 0, "cpu_m": None, "newest_write_s": None}, "0 of 3"),     # killed: execd's timeout, say
    ({"procs": 2, "cpu_m": 900.0}, "2 of 3"),                            # a worker died
    ({"procs": 3, "cpu_m": 0.0}, "0.0 m of CPU"),                        # alive but stopped
    ({"procs": 3, "cpu_m": 299.9}, "299.9 m of CPU"),
    ({"procs": 3, "cpu_m": None}, "None m of CPU"),                      # no window measured
])
def test_a_cpu_hog_that_does_not_spin_invalidates(probe, needle):
    running, why = hog_verdict("cpu", probe)
    assert running is False and needle in why


def test_disk_hogs():
    assert hog_verdict("disk", {"procs": 2, "cpu_m": 40.0, "newest_write_s": 0.3}) == (True, "")
    assert hog_verdict("disk", {"procs": 2, "newest_write_s": 20}) == (True, "")
    assert hog_verdict("disk", {"procs": 0, "newest_write_s": 0.2})[0] is False      # just killed: no
    assert "last write 20.1 s" in hog_verdict("disk", {"procs": 2, "newest_write_s": 20.1})[1]
    assert "last write None s" in hog_verdict("disk", {"procs": 2, "newest_write_s": None})[1]


def test_the_noisy_results_are_not_overwritten_by_a_narrower_run():
    previous = {"records": [{"runtime": "runc", "run": 1}, {"runtime": "kata", "run": 1}]}
    assert dropped_runtimes(previous, ["kata"]) == ["runc"]
    assert dropped_runtimes(previous, ["runc", "kata"]) == []
    assert dropped_runtimes(None, ["kata"]) == []
    assert dropped_runtimes({"records": []}, ["runc"]) == []
