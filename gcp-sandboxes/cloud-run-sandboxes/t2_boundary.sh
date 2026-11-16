# Test 2: the same questions asked on the host (the Cloud Run instance) and in a sandbox.
# Runs ON the instance. Run it with: ./run.sh t2_boundary.sh
SB=/usr/local/gcp/bin/sandbox
echo "=== HOST"
uname -a
echo "-- /proc/version: $(cut -c1-60 /proc/version)"
echo "-- dmesg:";   dmesg 2>&1 | head -3
echo "-- hypervisor flag in /proc/cpuinfo: $(grep -c -w hypervisor /proc/cpuinfo) of $(grep -c ^processor /proc/cpuinfo) CPUs"
grep -i -E '^(Seccomp|NoNewPrivs)' /proc/self/status
echo "-- PID 1: $(tr '\0' ' ' < /proc/1/cmdline)"
echo "-- mounts:"; mount | head -2 | cut -c1-120

echo; echo "=== SANDBOX"
# A sandbox starts with an empty environment (no PATH), so the shell and PATH are set explicitly
$SB do -- /bin/sh -c '
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
uname -a
echo "-- /proc/version: $(cut -c1-60 /proc/version)"
echo "-- dmesg:";   dmesg 2>&1 | head -3
echo "-- hypervisor flag in /proc/cpuinfo: $(grep -c -w hypervisor /proc/cpuinfo) of $(grep -c ^processor /proc/cpuinfo) CPUs"
grep -i -E "^(Seccomp|NoNewPrivs)" /proc/self/status
echo "-- PID 1: $(tr "\0" " " < /proc/1/cmdline)"
echo "-- mounts:"; mount | head -4 | cut -c1-120
'

echo; echo "=== What Cloud Run mounted, and how a sandbox is launched"
ls /usr/local/gcp/bin
$SB run probe --detach -- /bin/sleep 30 >/dev/null 2>&1
ps -eo args | grep -m1 '^runsc-sandbox' | cut -c1-190
$SB delete --force probe >/dev/null 2>&1
