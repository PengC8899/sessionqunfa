#!/bin/bash
set -e

# Configuration
VPS_HOST="54.251.239.79"
VPS_USER="ubuntu"
SSH_KEY="/Users/pclucky/qunfa/sessionqunfa/LightsailDefaultKey-ap-southeast-1 (2).pem"
REMOTE_DIR="/home/${VPS_USER}/sessionqunfa"
LOCAL_ENV=".env.vps"

# Ensure SSH key permissions
if [ -f "$SSH_KEY" ]; then
    chmod 600 "$SSH_KEY"
else
    echo "Error: SSH key not found at $SSH_KEY"
    exit 1
fi

echo "==> Preparing VPS environment..."

# SSH command to setup Docker and directories
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=accept-new "${VPS_USER}@${VPS_HOST}" "bash -s" << EOF
set -e

# Update and install dependencies
if ! command -v docker >/dev/null 2>&1; then
    echo "Installing Docker..."
    sudo apt-get update
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
        ca-certificates curl gnupg lsb-release rsync
    
    sudo mkdir -p /etc/apt/keyrings
    if [ ! -f /etc/apt/keyrings/docker.gpg ]; then
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    fi
    
    echo "deb [arch=\$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \$(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
    
    sudo apt-get update
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin rsync
    
    # Add user to docker group
    sudo usermod -aG docker \$USER
    echo "Docker installed."
else
    # Ensure rsync is installed
    if ! command -v rsync >/dev/null 2>&1; then
        sudo apt-get update
        sudo DEBIAN_FRONTEND=noninteractive apt-get install -y rsync
    fi
fi

# Create directory
mkdir -p "$REMOTE_DIR"
mkdir -p "$REMOTE_DIR/sessions"
mkdir -p "$REMOTE_DIR/data"
EOF

echo "==> Syncing files to VPS..."
# Sync files using rsync
# Exclude .git, sessions, data, etc.
rsync -avz --progress \
    -e "ssh -i \"$SSH_KEY\" -o StrictHostKeyChecking=accept-new" \
    --exclude '.git' \
    --exclude 'sessions' \
    --exclude 'data' \
    --exclude '__pycache__' \
    --exclude '.DS_Store' \
    --exclude 'vps-scripts' \
    . \
    "${VPS_USER}@${VPS_HOST}:${REMOTE_DIR}/"

echo "==> Uploading configuration..."
scp -i "$SSH_KEY" "$LOCAL_ENV" "${VPS_USER}@${VPS_HOST}:${REMOTE_DIR}/.env"

echo "==> Starting services..."
ssh -i "$SSH_KEY" "${VPS_USER}@${VPS_HOST}" "bash -s" << EOF
cd "$REMOTE_DIR"
# Force recreate to pick up changes
docker compose down --remove-orphans || true
docker compose up -d --build --remove-orphans
EOF

echo "==> Deployment complete!"
echo "Access your application at: http://${VPS_HOST}:8001"
