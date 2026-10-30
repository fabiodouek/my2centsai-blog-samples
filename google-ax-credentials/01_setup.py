"""Step 1: everything the cluster admin does once.

1. Build the task image (AX's runner + Claude Code, git, gh) and push it to ECR.
2. Store the real credentials as Kubernetes Secrets in their own namespace.
3. Let the atespace 'creds' resolve that namespace, and restart the provider.
4. Save the egress gateway's CA certificate for the tasks to trust.

This is the only script that reads the real credentials. They go to the
Kubernetes API, never into a task.
"""

import base64
import json

from common import (ATESPACE, HERE, IMAGE_REPO, INSTALL, RECORDS, SECRETS_NS, banner, kubectl,
                    load_env, run)

env = load_env()

banner("task image: ax-task-runner + Claude Code + gh")
base = next(l.split("=", 1)[1] for l in (INSTALL / ".records" / "source-and-images.txt").read_text().splitlines()
            if l.startswith("AX_DEFAULT_TASK_IMAGE="))
tag = f"{IMAGE_REPO}/ax/agent-runner:claude-code"
run(["docker", "buildx", "build", "--platform", "linux/amd64", "--push", "--quiet",
     "--build-arg", f"BASE={base}", "-t", tag, str(HERE / "image")], quiet=True)
digest = run(["docker", "buildx", "imagetools", "inspect", tag, "--format", "{{json .Manifest}}"],
             quiet=True).stdout
ref = f"{IMAGE_REPO}/ax/agent-runner@{json.loads(digest)['digest']}"
(RECORDS / "image.txt").write_text(ref + "\n")
print(f"  {ref}")

banner(f"secrets in namespace {SECRETS_NS}")
basic = base64.b64encode(f"x-access-token:{env['GITHUB_TOKEN']}".encode()).decode()
manifest = {"apiVersion": "v1", "kind": "List", "items": [
    {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": SECRETS_NS}},
    {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "anthropic", "namespace": SECRETS_NS},
     "stringData": {"api-key": env["ANTHROPIC_API_KEY"]}},
    {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "github", "namespace": SECRETS_NS},
     # gh and the REST API take a bearer token; git over HTTPS only speaks Basic,
     # and the gateway can't base64-encode, so the encoded pair is its own key.
     "stringData": {"token": env["GITHUB_TOKEN"], "basic": basic}},
    {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "canary", "namespace": SECRETS_NS},
     # A throwaway value for the mechanics tests, safe to see in an echo.
     "stringData": {"value": "canary-1111-not-a-real-credential"}},
]}
kubectl("apply", "-f", "-", input=json.dumps(manifest))

banner(f"namespace policy: atespace '{ATESPACE}' may resolve Secrets in '{SECRETS_NS}'")
policy = f"policies:\n- atespace: {ATESPACE}\n  allowedNamespaces:\n  - {SECRETS_NS}\n"
cm = kubectl("-n", "ate-system", "create", "configmap", "k8s-credential-provider-namespace-policy",
             f"--from-literal=namespace-policy.yaml={policy}", "--dry-run=client", "-o", "yaml", quiet=True).stdout
kubectl("apply", "-f", "-", input=cm)
print(policy)
# The provider reads the policy once at startup.
kubectl("-n", "ate-system", "rollout", "restart", "deployment/k8s-credential-provider")
kubectl("-n", "ate-system", "rollout", "status", "deployment/k8s-credential-provider", "--timeout=120s")

banner("the egress gateway's CA (public certificate, safe to hand to tasks)")
ctb = kubectl("get", "clustertrustbundles", "egress-mitm.ate.dev:mitm:primary-bundle",
              "-o", "jsonpath={.spec.trustBundle}", quiet=True).stdout
(RECORDS / "egress-ca.pem").write_text(ctb)
print(run(["openssl", "x509", "-noout", "-subject", "-enddate"], input=ctb, quiet=True).stdout.strip())
