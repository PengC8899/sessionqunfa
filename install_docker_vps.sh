#!/bin/bash
set -e

# Update and install prerequisites
sudo apt-get update
sudo apt-get install -y apt-transport-https ca-certificates curl software-properties-common gnupg lsb-release

# Install Docker
if ! command -v docker &> /dev/null; then
    echo "Installing Docker..."
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
    sudo apt-get update
    sudo apt-get install -y docker-ce docker-ce-cli containerd.io
    sudo usermod -aG docker $USER
    echo "Docker installed."
else
    echo "Docker already installed."
fi

# Install Docker Compose Plugin
if ! docker compose version &> /dev/null; then
    echo "Installing Docker Compose Plugin..."
    sudo apt-get install -y docker-compose-plugin
    echo "Docker Compose Plugin installed."
else
    echo "Docker Compose Plugin already installed."
fi

# Create app directory
mkdir -p ~/qunfa/data
mkdir -p ~/qunfa/sessions
mkdir -p ~/qunfa/static
mkdir -p ~/qunfa/templates
mkdir -p ~/qunfa/app

echo "Environment prepared."
