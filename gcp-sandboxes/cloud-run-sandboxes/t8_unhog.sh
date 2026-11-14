# Test 8 helper: stop every hog sandbox. Runs ON the instance.
SB=/usr/local/gcp/bin/sandbox
for i in $(seq 1 16); do $SB delete --force hog$i >/dev/null 2>&1; done
echo "left: $(ps -eo args | grep -c '^runsc-sandbox') runsc-sandbox processes"
