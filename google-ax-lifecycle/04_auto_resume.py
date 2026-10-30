"""Test 4: wake on request. A suspended task gets an HTTP request through
Substrate's atenet router. Does the router resume it, and how fast?"""

from common import delete, HERE, ax, banner, create, kubectl, run, status, timed, CONTEXT

NAME = "lc-wake"
ROUTER = "https://atenet-router.ate-system.svc"  # the name in the router's certificate
CURL = ("curl -s -m 60 -o /dev/null -w 'HTTP %{http_code} in %{time_total}s' "
        "--cacert /run/servicedns-ca/trust-bundle.pem "
        "--cert /run/podidentity/credential-bundle.pem --key /run/podidentity/credential-bundle.pem "
        f"-H 'ate-target-actor: lifecycle/{NAME}' {ROUTER}/readyz")

banner("the router has no plain-HTTP port (AX's networking doc shows port 80)")
kubectl("-n", "ate-system", "get", "svc", "atenet-router")

banner("a client the router trusts: a pod running as ax-server")
kubectl("-n", "ax-system", "delete", "pod", "router-probe", "--ignore-not-found", "--wait=true", quiet=True)
kubectl("apply", "-f", str(HERE / "router-probe.yaml"))
kubectl("-n", "ax-system", "wait", "--for=condition=Ready", "pod/router-probe", "--timeout=120s")
run(["sleep", "5"])  # let apk finish

banner(f"{NAME}: create, resume, suspend")
create(NAME)
ax("resume", "task", NAME, quiet=True)
ax("suspend", "task", NAME, quiet=True)
print(status(NAME))

for attempt in (1, 2):
    banner(f"request {attempt} to the suspended task, through the router")
    timed("GET /readyz", kubectl, "-n", "ax-system", "exec", "router-probe", "--", "sh", "-c", CURL)
    print(status(NAME))
    if attempt == 1:
        ax("suspend", "task", NAME, quiet=True)

banner("without the ate-target-actor header, or without a client certificate")
kubectl("-n", "ax-system", "exec", "router-probe", "--", "sh", "-c",
        CURL.replace(f"-H 'ate-target-actor: lifecycle/{NAME}' ", "") + "; echo")
kubectl("-n", "ax-system", "exec", "router-probe", "--", "sh", "-c",
        "curl -s -m 10 -o /dev/null -w 'no client cert: HTTP %{http_code}, curl exit ' "
        f"--cacert /run/servicedns-ca/trust-bundle.pem -H 'ate-target-actor: lifecycle/{NAME}' "
        f"{ROUTER}/readyz; echo $?", check=False)

kubectl("-n", "ax-system", "delete", "pod", "router-probe", "--wait=false", quiet=True)

delete(NAME)
