# Readable copy of the args embedded in pod-genpolicy.yaml. It runs in a plain pod on the
# m8g.xlarge node (Linux arm64) and reads the pod manifest from the ConfigMap
# confidential-manifest, mounted at /work/manifest.json.

dnf install -y -q tar gzip openssl findutils >/dev/null 2>&1; cd /tmp
U=https://d1uvmbvklg8rt.cloudfront.net/latest/aarch64
curl -fsSLO $U/nitro-sandboxes-runtime.tar.gz && curl -fsSLO $U/SHA256SUM && curl -fsSLO $U/SHA256SUM.sig
cat > nitro-sandboxes-runtime-v1.pub <<'PEM'
-----BEGIN PUBLIC KEY-----
MHYwEAYHKoZIzj0CAQYFK4EEACIDYgAESqdVXO82n+Zh5+IFLh1xjmxZLkjjZWvf
DCS927SdVYjP0AZ92XCxS+ucrs9cjuNlTaDARTHov2XVfMVgau8TEPFKGDRmVZXJ
c25CVihKHLFK8T8hNMIL+fbGcvE2fwIO
-----END PUBLIC KEY-----
PEM
openssl dgst -sha384 -verify nitro-sandboxes-runtime-v1.pub -signature SHA256SUM.sig SHA256SUM && sha256sum -c SHA256SUM --ignore-missing
tar -xzf nitro-sandboxes-runtime.tar.gz 2>/dev/null; GP=$(find /tmp -name eks-nitro-sandbox-genpolicy -type f | head -1); chmod +x $GP; $GP --version 2>&1 | head -1
$GP --profile confidential --with-pod-identity --image-architecture arm64 /work/manifest.json > policy.b64 2> gp.err; echo "exit=$? bytes=$(wc -c < policy.b64)"; cat gp.err | tail -15
echo POLICY_BEGIN; fold -w 4000 policy.b64; echo; echo POLICY_END
sleep infinity
