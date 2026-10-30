"""Test 5: auto-stop after idle_ttl_seconds=60, then what wakes a stopped sandbox (file read, command,
download URL, service URL) and how long that takes; does a running process or service traffic count as activity?"""

import time

import httpx

from common import banner, client, create, timed, wait_status


def idle_stop(sb, label: str) -> float:
    """Seconds from now (the caller's last activity) until the status endpoint says stopped."""
    s = wait_status(sb.name, "stopped", timeout=900, poll=2)
    print(f"[{s:6.1f}s] {label}: idle -> stopped")
    return s


def watch(sb, seconds: int, every: float = 5, poke=None) -> None:
    """Report status changes for a while, optionally calling poke() every 20 s."""
    t0, last, next_poke = time.perf_counter(), None, 0.0
    while (el := time.perf_counter() - t0) < seconds:
        if poke and el >= next_poke:
            poke()
            next_poke += 20
        st = client.get_sandbox_status(sb.name).status
        if st != last:
            print(f"  [{el:6.1f}s] {st}")
            last = st
        time.sleep(every)


sb = create("05", idle_ttl_seconds=60)
try:
    banner(f"Idle stop with idle_ttl_seconds=60: {sb.name}")
    sb.write("/root/wake.txt", "wake me\n")
    url = sb.generate_download_url("/root/wake.txt")  # minted while running, fetched after the stop
    idle_stop(sb, "after write()")

    banner("Wake triggers")
    with timed("wake via read()"):
        print(f"  read -> {sb.read('/root/wake.txt')!r}")
    print(f"  status: {client.get_sandbox_status(sb.name).status}")
    idle_stop(sb, "after read()")
    with timed("wake via run()"):
        print(f"  run -> {sb.run('cat /root/wake.txt').stdout!r}")
    idle_stop(sb, "after run()")
    with timed("wake via download URL (no credentials)"):
        r = httpx.get(url.download_url, timeout=120, follow_redirects=True)
        print(f"  HTTP {r.status_code} {r.text!r}")
    print(f"  status: {client.get_sandbox_status(sb.name).status}")
    idle_stop(sb, "after download")

    banner("Service URL on a stopped (cold-boot) sandbox")
    svc = sb.service(8000)
    with timed("GET service URL"):
        r = svc.get("/", timeout=120)
        print(f"  HTTP {r.status_code} {r.text.strip()[:120]!r}")
    print(f"  status: {client.get_sandbox_status(sb.name).status}")

    banner("Does a running process count as activity?")
    idle_stop(sb, "after the service request")
    sb.run("nohup sleep 900 >/dev/null 2>&1 &")
    idle_stop(sb, "detached 'nohup sleep 900 &' running")
    h = sb.run("sleep 900", wait=False, timeout=900)
    print(f"  attached 'sleep 900' via run(wait=False), handle held open; watching for 4 minutes")
    watch(sb, 240)
    h.kill()
finally:
    sb.delete()

sbm = create("05m", idle_ttl_seconds=60, preserve_memory_on_stop=True)
try:
    banner(f"Service URL wake with preserve_memory_on_stop: {sbm.name}")
    sbm.write("/root/index.html", "served from memory\n")
    # A backgrounded `nohup python3 -m http.server ... &` inside run() never returns (and ignores timeout=),
    # so start the server with wait=False and drop the handle: an open handle keeps the sandbox awake.
    server = sbm.run("python3 -m http.server 8000 --directory /root", wait=False, timeout=3600)
    del server
    time.sleep(2)
    svc = sbm.service(8000)
    print(f"  running: HTTP {svc.get('/index.html').status_code}")
    idle_stop(sbm, "after last request")
    with timed("wake via service URL"):
        r = svc.get("/index.html", timeout=120)
        print(f"  HTTP {r.status_code} {r.text.strip()!r}")
    print(f"  status: {client.get_sandbox_status(sbm.name).status}")

    banner("Does service-URL traffic every 20 s keep it awake?")
    watch(sbm, 240, poke=lambda: svc.get("/index.html", timeout=120))
    banner("Traffic stops")
    idle_stop(sbm, "after last service request")
finally:
    sbm.delete()
