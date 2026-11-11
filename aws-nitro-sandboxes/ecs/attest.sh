# Readable copy of the command embedded in td-confidential.json, td-attest-isolated.json
# and td-tampered.json. In the task definitions it is one line, these lines joined with "; ":
# ecs-nitro-sandbox-genpolicy rejects process arguments that contain a newline.
# NSM_PY_B64 (../nsm/nsm.py, base64) and SECRET_CT (KMS ciphertext) come from the environment.

dnf install -y -q openssl awscli-2 python3 >/dev/null 2>&1
echo "$NSM_PY_B64" | base64 -d > /tmp/nsm.py; cd /tmp
echo "kernel: $(uname -rv)"; ls -la /dev/nsm 2>&1
openssl genrsa -out key.pem 2048 2>/dev/null && openssl rsa -in key.pem -pubout -outform DER -out pub.der 2>/dev/null
python3 nsm.py attest pub.der att.doc && python3 nsm.py pcrs att.doc | grep -E "module_id|PCR0:|PCR8:|PCR17:"
echo "$SECRET_CT" | base64 -d > ct.bin
if aws kms decrypt --region us-east-1 --ciphertext-blob fileb://ct.bin --recipient "KeyEncryptionAlgorithm=RSAES_OAEP_SHA_256,AttestationDocument=$(base64 -w0 att.doc)" --query CiphertextForRecipient --output text > cfr.b64 2> err.txt; then base64 -d cfr.b64 > cfr.der; echo "SECRET: $(openssl cms -decrypt -inform DER -in cfr.der -inkey key.pem)"; else echo "KMS: $(sed 's/User: arn[^ ]* //' err.txt | head -c 260)"; fi
echo ATTEST_DONE; sleep 3600
