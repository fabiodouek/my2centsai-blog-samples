# Test 7: startup latency against Google's "1,000 sandboxes, about 500 ms on average".
# Runs ON the instance: N=1000 CONC_N=300 ./run.sh t7_latency.sh 1700   (N, CONC_N: see below)
SB=/usr/local/gcp/bin/sandbox
N=${N:-1000}        # sandboxes in the sequential run
C=${CONC_N:-$N}     # sandboxes in each concurrent run
OUT=/tmp/t7; rm -rf $OUT; mkdir -p $OUT

stats() {  # stats <file of milliseconds>
  sort -n "$1" | awk '{a[NR]=$1; s+=$1} END {printf "n=%d mean=%.0f ms  p50=%d  p95=%d  p99=%d  max=%d\n", NR, s/NR, a[int(NR*0.5)], a[int(NR*0.95)], a[int(NR*0.99)], a[NR]}'
}
one() {  # one <i>: time one sandbox that starts, runs /bin/true and is deleted; prints milliseconds, or FAIL
  local a b err
  a=$(date +%s%N)
  if err=$($SB do -- /bin/true 2>&1); then b=$(date +%s%N); echo $(( (b - a) / 1000000 )); else echo "FAIL $(echo "$err" | tr '\n' ' ' | cut -c1-150)"; fi
}
export -f one; export SB

echo "=== (a) sequential, n=$N: sandbox do -- /bin/true"
t0=$(date +%s%N)
for i in $(seq $N); do one $i; [ $((i % 100)) = 0 ] && echo "  $i of $N sandboxes done" >&2; done > $OUT/seq.txt
t1=$(date +%s%N)
echo "failures: $(grep -c FAIL $OUT/seq.txt)"; grep -v FAIL $OUT/seq.txt > $OUT/seq.ok; grep -m1 FAIL $OUT/seq.txt
stats $OUT/seq.ok
echo "wall time: $(( (t1 - t0) / 1000000 )) ms"

for P in 10 50 100; do
  echo; echo "=== (b) $C sandboxes, $P at a time"
  t0=$(date +%s%N)
  seq $C | xargs -P $P -I{} bash -c 'one {}' > $OUT/p$P.txt
  t1=$(date +%s%N); W=$(( (t1 - t0) / 1000000 ))
  echo "failures: $(grep -c FAIL $OUT/p$P.txt)"; grep -v FAIL $OUT/p$P.txt > $OUT/p$P.ok; grep -m1 FAIL $OUT/p$P.txt
  stats $OUT/p$P.ok
  echo "wall time: $W ms  =>  $(( C * 1000 / W )) sandboxes/s,  $(( W / C )) ms per sandbox of wall time"
done

echo; echo "=== (c) warm reuse: one long-lived sandbox, 100 x exec"
$SB delete --force warm >/dev/null 2>&1
$SB run warm --detach -- /bin/sleep 12m >/dev/null 2>&1
sleep 1
for i in $(seq 100); do a=$(date +%s%N); $SB exec warm -- /bin/true >/dev/null 2>&1; b=$(date +%s%N); echo $(( (b - a) / 1000000 )); done > $OUT/warm.txt
stats $OUT/warm.txt
$SB delete --force warm >/dev/null 2>&1
