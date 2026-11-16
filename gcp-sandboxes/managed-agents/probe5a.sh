# probe5a.sh: runs inside the sandbox. Writes a marker file in four places, installs a pip package and
# starts a background process, so probe5b.sh can check what survives an idle restore.
cd /; for d in / /root /tmp /opt; do echo kept > $d/marker.txt; done
pip install -q cowsay 2>/dev/null && echo "cowsay installed"
nohup sleep 100000 >/dev/null 2>&1 & echo "background process started"
echo "uptime: $(cut -d' ' -f1 /proc/uptime) s"
