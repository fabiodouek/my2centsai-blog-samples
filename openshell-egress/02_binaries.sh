#!/usr/bin/env bash
# Test 2: binary-scoped rules. pypi.org is allowed for /usr/bin/curl only. python3 is denied, so is
# a copy of curl under another path, and a pinned binary is denied once its contents change
# (OpenShell records a SHA-256 of each binary the first time it connects).
source "$(dirname "$0")/lib.sh"
NAME="bins"
cleanup() { os sandbox delete "$NAME" >/dev/null 2>&1; }
trap cleanup EXIT

say "openshell sandbox create --name ${NAME} --policy policies/curl-pypi.yaml"
make_sandbox "$NAME" --policy "${HERE}/policies/curl-pypi.yaml" || exit 1
code() { in_sb "$NAME" sh -c "$1 -sS -m 8 -o /dev/null -w '%{http_code}' https://pypi.org/simple/ 2>/dev/null || true"; }

say "the same host from three binaries"
printf '  %-30s %s\n' "/usr/bin/curl" "$(code /usr/bin/curl)"
printf '  %-30s %s\n' "python3" "$(in_sb "$NAME" python3 -c 'import urllib.request as u; print(u.urlopen("https://pypi.org/simple/", timeout=8).status)' 2>&1 | tail -n 1)"
in_sb "$NAME" sh -c 'cp /usr/bin/curl /sandbox/curl-copy'
printf '  %-30s %s\n' "/sandbox/curl-copy (a copy)" "$(code /sandbox/curl-copy)"
sleep 3   # let the supervisor flush its log
ocsf "$NAME" | grep -E 'DENIED' | cut -c1-120 | sed 's/^/  /' | tail -n 2
echo "  why python3 is denied:"
os logs "$NAME" -n 100 --level warn 2>/dev/null | nocolor | grep -o "reason=binary '[^']*' not allowed[^)]*)" | head -n 1 | sed 's/^/    /'

say "binary pinning: /sandbox/bin/fetch is allowed by path"
in_sb "$NAME" sh -c 'mkdir -p /sandbox/bin && cp /usr/bin/curl /sandbox/bin/fetch'
printf '  %-30s %s\n' "fetch, first use" "$(code /sandbox/bin/fetch)"
printf '  %-30s %s\n' "fetch, second use" "$(code /sandbox/bin/fetch)"
in_sb "$NAME" sh -c 'cp /usr/bin/curl /tmp/x && printf "\n" >> /tmp/x && cp /tmp/x /sandbox/bin/fetch && sha256sum /usr/bin/curl /sandbox/bin/fetch | awk "{print \"  sha256 \" substr(\$1,1,12) \"  \" \$2}"'
printf '  %-30s %s\n' "fetch, one byte changed" "$(code /sandbox/bin/fetch)"
sleep 3
os logs "$NAME" -n 100 --level warn 2>/dev/null | nocolor | grep '/sandbox/bin/fetch' | grep -o 'binary integrity violation.*' | sed 's/^/  WARN: /' | tail -n 1
# The OCSF event for it names no binary ("-"): the identity check failed before a name was known.
ocsf "$NAME" | grep 'identity_unavailable' | cut -c1-110 | sed 's/^/  OCSF: /' | tail -n 1
