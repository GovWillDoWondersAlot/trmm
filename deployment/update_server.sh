#!/usr/bin/env bash
set -e

echo "=========================================================="
echo "  [*] Tactical RMM — Live Server Synchronizer & Updater"
echo "=========================================================="

# 1. Ensure required Android APK signing packages are present
echo "[1/4] Installing / verifying signing tools (Java 17, apksigner, zipalign)..."
sudo apt-get update -y
sudo apt-get install -y openjdk-17-jdk-headless apksigner zipalign

# 2. Navigate to repository directory
REPO_DIR="$HOME/TRMM"
if [ ! -d "$REPO_DIR" ]; then
    REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi
cd "$REPO_DIR"
echo "[2/4] Synchronizing repository at: $REPO_DIR"

# 3. Fetch latest commit and reset cleanly
git fetch origin main
git reset --hard origin/main

# Ensure generated_agents directory exists and permissions are correct
mkdir -p "$REPO_DIR/generated_agents"
sudo chown -R ubuntu:ubuntu "$REPO_DIR"

# 4. Restart systemd daemon cleanly
echo "[3/4] Reloading systemd and restarting trmm.service..."
sudo systemctl daemon-reload
sudo systemctl restart trmm

echo "[4/4] Checking service status..."
sudo systemctl status trmm --no-pager -n 10

echo ""
echo "=========================================================="
echo "  [✓] Tactical RMM Server successfully updated and active!"
echo "=========================================================="
