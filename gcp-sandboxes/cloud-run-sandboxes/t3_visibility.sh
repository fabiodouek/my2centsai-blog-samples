# Test 3: what a sandbox can see of the host. Runs ON the instance (./run.sh t3_visibility.sh).
# The service has: env PLAIN=visible, secret DUMMY_SECRET (as env), the same secret mounted at
# /secrets/dummy, and the image has a root-only 0600 file /etc/m2c-0600.
SB=/usr/local/gcp/bin/sandbox
P='export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'

echo "=== HOST sees"
echo "PLAIN=$PLAIN  DUMMY_SECRET=$DUMMY_SECRET"
echo "/secrets/dummy: $(cat /secrets/dummy)"
echo "/etc/m2c-0600:  $(cat /etc/m2c-0600)"

echo; echo "=== SANDBOX: environment, user, files"
$SB do -- /bin/sh -c "$P"'
echo "-- env:";  env | sort
echo "-- id:";   id
echo "-- sudo -n true:"; sudo -n true 2>/dev/null && echo "sudo works" || echo "sudo failed (it cannot even read /etc/sudoers)"
echo "-- ls -l /etc/m2c-0600:"; ls -l /etc/m2c-0600
echo "-- cat /etc/m2c-0600:";   cat /etc/m2c-0600 2>&1
echo "-- ls -l /secrets:";      ls -l /secrets 2>&1
echo "-- cat /secrets/dummy:";  cat /secrets/dummy 2>&1; echo
echo "-- processes I can see:"; ps -e 2>&1
'

echo; echo "=== Metadata server: host, then sandbox without and with --allow-egress (HTTP status only)"
M='http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/email'
echo "host:                    $(curl -m3 -s -o /dev/null -w '%{http_code}' -H 'Metadata-Flavor: Google' $M)"
echo "sandbox:                 $($SB do -- /usr/bin/curl -m3 -sS -o /dev/null -w '%{http_code}' -H 'Metadata-Flavor: Google' $M 2>&1 | head -1 | cut -c1-72)"
echo "sandbox --allow-egress:  $($SB do --allow-egress -- /usr/bin/curl -m3 -sS -o /dev/null -w '%{http_code}' -H 'Metadata-Flavor: Google' $M 2>&1 | head -1 | cut -c1-72)"
echo "sandbox --allow-egress, metadata.google.internal: $($SB do --allow-egress -- /usr/bin/curl -m3 -sS -o /dev/null -w '%{http_code}' -H 'Metadata-Flavor: Google' http://metadata.google.internal/computeMetadata/v1/project/project-id 2>&1 | head -1 | cut -c1-72)"
