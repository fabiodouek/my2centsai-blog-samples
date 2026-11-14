# Test 8b: one sandbox allocates memory until it is killed. Runs ON the instance.
# Question: what dies (the sandbox, the host server, the whole instance) and with which exit code?
SB=/usr/local/gcp/bin/sandbox
echo "host before: server PID $(pgrep -f 'app/server.py' | head -1), $(grep MemAvailable /proc/meminfo | tr -s ' ')"
$SB do -- /usr/local/bin/python3 -c '
b = []
n = 0
while True:
    b.append(bytearray(64 * 2**20)); n += 1
    for blk in b[-1:]: blk[::4096] = b"x" * len(blk[::4096])   # touch every page
    if n % 8 == 0: print(f"allocated {n * 64} MiB", flush=True)
' 2>&1 | tail -4
echo "sandbox exit status: ${PIPESTATUS[0]}"
echo "host after:  server PID $(pgrep -f 'app/server.py' | head -1), uptime $(cut -d' ' -f1 /proc/uptime) s, $(grep MemAvailable /proc/meminfo | tr -s ' ')"
