"""Run commands in an interactive Cloud Run SSH session and print the session (Preview feature).

usage: python3 ssh_run.py SERVICE 'command; command' [--script FILE] [--seconds 90]

--script FILE is copied into the instance first (typed in with echo off) and run as /tmp/s.sh after the commands.

`gcloud beta run services ssh` has no --command flag and needs a terminal, so this opens it in a
pseudo-terminal, types the commands, and prints the session. The banner lines that name the project,
instance and image are dropped. The service must be deployed with --ssh (see deploy.sh).
"""
import os, pty, re, select, sys, time

service, commands = sys.argv[1], sys.argv[2]
limit = int(sys.argv[sys.argv.index("--seconds") + 1]) if "--seconds" in sys.argv else 90
script = open(sys.argv[sys.argv.index("--script") + 1]).read() if "--script" in sys.argv else None
pid, fd = pty.fork()
if pid == 0:
    os.execvp("gcloud", ["gcloud", "beta", "run", "services", "ssh", service])
buf, sent, t0 = b"", False, time.time()
while time.time() - t0 < limit:
    ready, _, _ = select.select([fd], [], [], 1)
    if ready:
        try:
            data = os.read(fd, 8192)
        except OSError:
            break
        if not data:
            break
        buf += data
    if not sent and b"root@" in buf:
        if script:
            os.write(fd, ("stty -echo; cat > /tmp/s.sh <<'M2C_EOF'\n" + script + "M2C_EOF\nstty echo\n").encode())
            time.sleep(1)
        os.write(fd, (commands + ("\nbash /tmp/s.sh" if script else "") + "\nexit\n").encode()); sent = True
text = buf.decode(errors="replace").replace("\r", "")
text = text[text.find("root@"):]
if script:  # hide the echo of the script being typed in
    text = re.sub(r"root@localhost:~# stty -echo.*?(root@localhost:~# bash /tmp/s\.sh)", r"\1", text, flags=re.S)
print("\n".join(l for l in text.split("\n") if not l.startswith(("Connection to", "logout"))))
