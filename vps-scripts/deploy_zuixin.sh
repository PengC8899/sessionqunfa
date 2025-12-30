#!/bin/bash
set -e

# 自动化部署到 Lightsail VPS (zuixin-2)
#
# 使用方式（在本机运行）：
#   cd /Users/pclucky/qunfa/sessionqunfa
#   bash vps-scripts/deploy_zuixin.sh
#
# 可通过环境变量覆盖默认配置：
#   VPS_HOST, VPS_USER, SSH_KEY, REMOTE_DIR, LOCAL_ENV

VPS_HOST="${VPS_HOST:-54.251.239.79}"
VPS_USER="${VPS_USER:-ubuntu}"
SSH_KEY="${SSH_KEY:-/Users/pclucky/qunfa/sessionqunfa/zuixin.pem}"
REMOTE_DIR="${REMOTE_DIR:-/home/${VPS_USER}/sessionqunfa}"
LOCAL_ENV="${LOCAL_ENV:-/Users/pclucky/qunfa/sessionqunfa/.env}"

if [ ! -f "${SSH_KEY}" ]; then
  echo "SSH 密钥不存在: ${SSH_KEY}"
  exit 1
fi

if [ ! -f "${LOCAL_ENV}" ]; then
  echo "本地 .env 文件不存在: ${LOCAL_ENV}"
  exit 1
fi

echo "==> 使用密钥 ${SSH_KEY}"
chmod 600 "${SSH_KEY}"

echo "==> 第一步：在 VPS 上安装 Docker，并拉取/更新代码..."
ssh -i "${SSH_KEY}" -o StrictHostKeyChecking=accept-new "${VPS_USER}@${VPS_HOST}" "REMOTE_DIR='${REMOTE_DIR}' bash -s" << 'EOF'
set -e

if ! command -v docker >/dev/null 2>&1; then
  sudo apt-get update
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
    ca-certificates \
    curl \
    gnupg \
    lsb-release
  if ! [ -f /usr/share/keyrings/docker-archive-keyring.gpg ]; then
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg
  fi
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
  sudo usermod -aG docker "$USER" || true
fi

if [ ! -d "${REMOTE_DIR}" ]; then
  mkdir -p "${REMOTE_DIR%/*}"
  git clone https://github.com/PengC8899/sessionqunfa.git "${REMOTE_DIR}"
else
  cd "${REMOTE_DIR}"
  git fetch --all --prune
  git reset --hard origin/main
fi

mkdir -p "${REMOTE_DIR}/sessions" "${REMOTE_DIR}/data"
EOF

echo "==> 第二步：上传本机 .env 到 VPS..."
scp -i "${SSH_KEY}" "${LOCAL_ENV}" "${VPS_USER}@${VPS_HOST}:${REMOTE_DIR}/.env"

echo "==> 第三步：在 VPS 上启动 Docker 服务（web 容器）..."
ssh -i "${SSH_KEY}" "${VPS_USER}@${VPS_HOST}" bash << EOF
set -e
cd "${REMOTE_DIR}"
docker compose up -d --build web
EOF

echo "==> 部署完成"
echo "VPS 地址: http://${VPS_HOST}:8001/"
echo "如需通过域名 + HTTPS 访问，可再按文档配置 Caddy 或 Nginx 反向代理。"
