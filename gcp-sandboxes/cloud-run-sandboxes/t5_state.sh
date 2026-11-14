# Test 5: filesystem and state. Runs ON the instance (./run.sh t5_state.sh).
SB=/usr/local/gcp/bin/sandbox
P='export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin;'
step() { echo; echo "--- $1"; }

step "write outside /tmp, no --write"
$SB do -- /bin/sh -c "$P touch /etc/x && echo wrote /etc/x" 2>&1 | head -2
step "write /tmp/x, no --write"
$SB do -- /bin/sh -c "$P touch /tmp/x && echo wrote /tmp/x" 2>&1 | head -2
step "--write: write /etc/x, then look for it on the host afterwards"
$SB do --write -- /bin/sh -c "$P touch /etc/x && echo wrote /etc/x" 2>&1 | head -2
ls -l /etc/x 2>&1 | head -1

step "export a tar: --write --export-tar, then import it into a new sandbox"
rm -f /tmp/w.tar
$SB do --write --export-tar=/tmp/w.tar -- /bin/sh -c "$P echo a > /root/f; echo b > /tmp/g; echo wrote /root/f and /tmp/g" 2>&1 | head -2
echo "tar contents:"; tar tf /tmp/w.tar 2>&1 | head
$SB do --write --import-tar=/tmp/w.tar -- /bin/sh -c "$P echo -n '/root/f: '; cat /root/f; echo -n '/tmp/g: '; cat /tmp/g" 2>&1 | head -4

step "fork: does the counter keep running in the copy, and does the copy have the files?"
$SB delete --force w1 >/dev/null 2>&1; $SB delete --force w2 >/dev/null 2>&1
$SB run w1 --detach --write -- /bin/sh -c 'i=0; while :; do i=$((i+1)); echo $i > /root/c; sleep 1; done' >/dev/null 2>&1   # detached: do not let it hold our output pipe
sleep 5
$SB exec w1 -- /bin/sh -c "echo hello > /root/marker"   # a plain file, to see whether the copy has it
echo "w1 counter after 5 s: $($SB exec w1 -- /bin/cat /root/c)"
$SB fork --detach w1 w2 >/dev/null 2>&1
echo "w2 counter right after fork: $($SB exec w2 -- /bin/cat /root/c 2>&1)"
sleep 3
echo "w2 counter 3 s later:        $($SB exec w2 -- /bin/cat /root/c 2>&1)"
echo "marker file in w2:             $($SB exec w2 -- /bin/cat /root/marker 2>&1 | head -1)"
echo "w1 counter now:              $($SB exec w1 -- /bin/cat /root/c 2>&1)"
$SB delete --force w1; $SB delete --force w2

step "--rootfs /tmp"
$SB do --rootfs /tmp -- /bin/true 2>&1 | head -3; echo "exit ${PIPESTATUS[0]}"
