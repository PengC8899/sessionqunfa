#!/bin/bash
set -e

sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  apt-transport-https \
  ca-certificates \
  curl \
  software-properties-common \
  gnupg \
  lsb-release

if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
  sudo usermod -aG docker "$USER" || true
else
  if ! docker compose version >/dev/null 2>&1; then
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y docker-compose-plugin
  fi
fi

mkdir -p ~/qunfa/data
mkdir -p ~/qunfa/sessions
mkdir -p ~/qunfa/static
mkdir -p ~/qunfa/templates
mkdir -p ~/qunfa/app

sudo chown -R "$USER":"$USER" ~/qunfa

if ! sudo swapon --show | grep -q swapfile; then
  sudo fallocate -l 1G /swapfile || sudo dd if=/dev/zero of=/swapfile bs=1M count=1024
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  if ! grep -q "/swapfile" /etc/fstab; then
    echo "/swapfile none swap sw 0 0" | sudo tee -a /etc/fstab >/dev/null
  fi
fi

echo "Environment prepared."
