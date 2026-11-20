#!/usr/bin/env bash
# On the instance, as root: swap, Docker + buildx, a non-root user, and the Enclave arm64 binary.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
VERSION="v1.0.1"
BASE="https://github.com/eclipse-enclave/enclave/releases/download/$VERSION"

echo "== host"; uname -m; grep PRETTY /etc/os-release; free -m | sed -n 1,2p

echo "== swap (2 GiB RAM is tight for an image build)"
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile; chmod 600 /swapfile; mkswap /swapfile >/dev/null; swapon /swapfile
fi
swapon --show

echo "== docker + buildx"
apt-get update -qq
apt-get install -y -qq docker.io docker-buildx git curl jq >/dev/null
systemctl enable --now docker
docker --version; docker buildx version; git --version

echo "== user for the agent (Enclave refuses to run as root)"
id agent >/dev/null 2>&1 || useradd -m -s /bin/bash agent
usermod -aG docker agent
sudo -u agent git config --global user.name "Enclave Eval"
sudo -u agent git config --global user.email "enclave-eval@example.invalid"

echo "== enclave $VERSION (linux arm64)"
cd /tmp && curl -fsSLO "$BASE/enclave-linux-arm64" -O "$BASE/checksums.txt"
grep ' enclave-linux-arm64$' checksums.txt | sha256sum -c -
install -m 0755 enclave-linux-arm64 /usr/local/bin/enclave
sudo -u agent enclave --version
