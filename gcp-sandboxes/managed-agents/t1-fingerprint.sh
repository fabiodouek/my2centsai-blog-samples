# Test 1: what is the sandbox? Starts a fresh environment and runs probe1.sh in it.
source lib.sh
echo "### A fresh sandbox: what is it?"
ask '{"type": "remote"}' probe1.sh
