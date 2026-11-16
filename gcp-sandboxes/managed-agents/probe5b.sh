# probe5b.sh: runs inside the same sandbox after it has been idle. Uptime near zero means a restored sandbox.
echo "uptime: $(cut -d' ' -f1 /proc/uptime) s"
for d in / /root /tmp /opt; do printf '%-6s %s\n' $d "$(cat $d/marker.txt 2>/dev/null || echo MISSING)"; done
python3 -c "import cowsay" 2>/dev/null && echo "cowsay  kept" || echo "cowsay  MISSING"
pgrep -f "sleep 100000" >/dev/null && echo "process kept" || echo "process gone"
