#!/bin/bash
# Install Docker Engine + compose; student can use docker without sudo
# From Docker's apt repository (Debian / Ubuntu). Safe to run again.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

. /etc/os-release                                   # ID=ubuntu|debian, VERSION_CODENAME
apt-get update -q -o DPkg::Lock::Timeout=300
apt-get install -y -q -o DPkg::Lock::Timeout=300 ca-certificates curl
install -m 0755 -d /etc/apt/keyrings
curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc]" \
     "https://download.docker.com/linux/$ID $VERSION_CODENAME stable" > /etc/apt/sources.list.d/docker.list
apt-get update -q -o DPkg::Lock::Timeout=300
apt-get install -y -q -o DPkg::Lock::Timeout=300 docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
apt-get clean                                       # the downloaded .debs: not needed any more

systemctl enable --now docker
usermod -aG docker student                          # works after the student logs in again

docker run --rm hello-world | grep -q "Hello from Docker"
docker --version
docker compose version
