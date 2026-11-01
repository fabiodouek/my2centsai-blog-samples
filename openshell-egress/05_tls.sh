#!/usr/bin/env bash
# Test 5: TLS termination. The supervisor terminates TLS on every allowed connection (unless the
# endpoint says tls: skip) with a CA it creates for the sandbox. Which tools trust it, and which
# environment variable each one needs.
source "$(dirname "$0")/lib.sh"
NAME="tls"
cleanup() { os sandbox delete "$NAME" >/dev/null 2>&1; }
trap cleanup EXIT

say "openshell sandbox create --name ${NAME} --policy policies/tls.yaml"
make_sandbox "$NAME" --policy "${HERE}/policies/tls.yaml" || exit 1

say "the certificate curl sees for pypi.org"
in_sb "$NAME" sh -c 'curl -sS -m 8 -v -o /dev/null https://pypi.org/simple/ 2>&1 | grep -E "^\*  (subject|issuer):" | sed "s/^\*  /  /"'
say "the sandbox CA, and the variables that point at it"
in_sb "$NAME" sh -c 'openssl x509 -in /run/openshell-supervisor-ca/material/ca.crt -noout -subject -enddate | sed "s/^/  /"
  env | grep -E "^(SSL_CERT_FILE|REQUESTS_CA_BUNDLE|CURL_CA_BUNDLE|GIT_SSL_CAINFO|NODE_EXTRA_CA_CERTS|DENO_CERT|AWS_CA_BUNDLE)=" | sort | sed "s/^/  /"
  echo "  AWS_CA_BUNDLE: ${AWS_CA_BUNDLE:-not set}"'

say "each tool, as is and without the variable it reads"
# Prints "ok", or the TLS error the tool reported.
tls_check() { in_sb "$NAME" sh -c "$1" 2>&1 | grep -oE 'CERTIFICATE_VERIFY_FAILED|SSL certificate problem: [a-z -]+|server certificate verification failed|^ok$' | head -n 1; }
row() { printf '  %-37s %-6s %s\n' "$1" "$(tls_check "$2")" "$(tls_check "$3")"; }
CURL='curl -sS -m 8 -o /dev/null https://pypi.org/simple/ && echo ok'
PY='python3 -c "import urllib.request as u; u.urlopen(\"https://pypi.org/simple/\", timeout=8)" && echo ok'
GIT='git ls-remote https://pypi.org/simple/ 2>&1 | grep -q "not found" && echo ok || git ls-remote https://pypi.org/simple/'
AWS='aws s3 ls s3://noaa-ghcn-pds/ --no-sign-request >/dev/null && echo ok'
printf '  %-37s %-6s %s\n' "" "as is" "without the variable"
row "curl (CURL_CA_BUNDLE, SSL_CERT_FILE)" "$CURL" "unset CURL_CA_BUNDLE SSL_CERT_FILE; $CURL"
row "python3 (SSL_CERT_FILE)" "$PY" "unset SSL_CERT_FILE; $PY"
row "git (GIT_SSL_CAINFO)" "$GIT" "unset GIT_SSL_CAINFO; $GIT"
row "AWS CLI 2.39 (REQUESTS_CA_BUNDLE)" "$AWS" "unset REQUESTS_CA_BUNDLE; $AWS"
