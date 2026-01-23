#!/bin/bash
# Aerie deployment script
# Run this on the server to update and restart the application

set -e

cd /opt/aerie

echo "==> Pulling latest changes..."
git pull origin main

echo "==> Building containers..."
docker compose build

echo "==> Restarting services..."
docker compose up -d

echo "==> Deployment complete. Showing recent logs..."
docker compose logs -f --tail=50
