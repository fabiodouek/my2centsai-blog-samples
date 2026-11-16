# Runs INSIDE the ADK service's instance (the image has no ps): which sandbox processes exist, and do they use CPU?
cpu() { t=0; for p in $(for d in /proc/[0-9]*; do c=$(tr '\0' ' ' < $d/cmdline 2>/dev/null); case "$c" in *runsc*) echo ${d#/proc/};; esac; done); do
          v=$(awk '{print $14+$15}' /proc/$p/stat 2>/dev/null); t=$((t + ${v:-0})); done; echo $t; }
echo "load average: $(cut -d' ' -f1-3 /proc/loadavg)"
for d in /proc/[0-9]*; do c=$(tr '\0' ' ' < $d/cmdline 2>/dev/null | cut -c1-100); case "$c" in *"sandbox do"*|*runsc*) echo "PID ${d#/proc/}  $c";; esac; done
a=$(cpu); sleep 5; b=$(cpu)
echo "CPU used by the runsc processes over 5 s: $(( (b - a) / 5 )) ticks/s (100 = one full CPU)"
