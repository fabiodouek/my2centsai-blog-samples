# probe1.sh: runs inside the sandbox. Prints the kernel (gVisor reports itself here), the OS image,
# the user, CPU count, memory, and the egress proxy the environment points to.
uname -r
grep PRETTY_NAME /etc/os-release
echo "user: $(id -un), cpus: $(nproc), ram: $(free -g | awk 'NR==2{print $2}') GiB"
env | grep '^HTTPS_PROXY='
