# Test 8 helper. Runs ON the instance (t8_noisy.sh sends it with FIRST= and LAST= lines in front).
SB=/usr/local/gcp/bin/sandbox
# start sandboxes hog<first>..hog<last>, each a detached busy loop
for i in $(seq ${FIRST:-1} ${LAST:-4}); do
  $SB run hog$i --detach -- /bin/sh -c 'while :; do :; done' >/dev/null 2>&1
done
echo "hogs running: $(ps -eo args | grep -c '^runsc-sandbox') runsc-sandbox processes; load: $(cut -d' ' -f1-3 /proc/loadavg)"
