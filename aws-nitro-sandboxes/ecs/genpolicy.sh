# Readable copy of the command embedded in td-genpolicy.json. It runs as a SHARED task on
# the cluster (Linux arm64), so you do not need a Linux machine to generate the policy.
# TD_B64 is the confidential task definition, base64; PUBKEY is the release signing key
# published in the ECS docs (Getting the CLI tools).

dnf install -y -q tar gzip openssl findutils >/dev/null 2>&1; cd /tmp
U=https://d1uvmbvklg8rt.cloudfront.net/latest/aarch64
curl -fsSLO $U/nitro-sandboxes-runtime.tar.gz && curl -fsSLO $U/SHA256SUM && curl -fsSLO $U/SHA256SUM.sig
printf '%s' "$PUBKEY" > nitro-sandboxes-runtime-v1.pub
openssl dgst -sha384 -verify nitro-sandboxes-runtime-v1.pub -signature SHA256SUM.sig SHA256SUM && sha256sum -c SHA256SUM --ignore-missing
tar -xzf nitro-sandboxes-runtime.tar.gz 2>/dev/null; GP=$(find /tmp -name ecs-nitro-sandbox-genpolicy -type f | head -1); chmod +x $GP; echo "genpolicy: $GP"; $GP --version 2>&1 | head -2
echo "$TD_B64" | base64 -d > td.json
$GP --image-architecture arm64 --task-definition td.json > policy.b64 2> gp.err; echo "exit=$? bytes=$(wc -c < policy.b64)"; tail -3 gp.err
echo POLICY_BEGIN; fold -w 4000 policy.b64; echo; echo POLICY_END
$GP --image-architecture arm64 --task-definition td.json --output pcrs 2>&1 | tail -12
echo RUNNER_DONE; sleep 600

