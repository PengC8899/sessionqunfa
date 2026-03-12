#!/bin/bash

# Configuration for 8002
export COMPOSE_PROJECT_NAME="sessionqunfa_8002"
export WEB_PORT=8002
export CADDY_HTTP_PORT=8082
export CADDY_HTTPS_PORT=8442
export HOST_SESSION_DIR="./sessions_8002"
export HOST_DATA_DIR="./data_8002"

echo "==== Deploying SessionQunfa on Port 8002 ===="
echo "Project Name: $COMPOSE_PROJECT_NAME"
echo "Web Port: $WEB_PORT"
echo "Session Dir: $HOST_SESSION_DIR"
echo "Data Dir: $HOST_DATA_DIR"

# Create directories if not exist
mkdir -p "$HOST_SESSION_DIR"
mkdir -p "$HOST_DATA_DIR"

# Deploy
docker compose -p "$COMPOSE_PROJECT_NAME" up -d --build

echo "==== Deployment Complete ===="
echo "Access at: http://localhost:$WEB_PORT"
