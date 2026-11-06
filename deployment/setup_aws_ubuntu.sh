#!/usr/bin/env bash
# ==============================================================================
# Tactical RMM — Automated AWS EC2 (Ubuntu 22.04 / 24.04 LTS) Server Installer
# ==============================================================================
set -e

echo "=================================================================="
echo "  [*] Tactical RMM - AWS EC2 Automated Deployment Setup"
echo "=================================================================="

# Check if running as non-root with sudo or root
if [ "$EUID" -eq 0 ]; then
    INSTALL_USER="${SUDO_USER:-ubuntu}"
    INSTALL_HOME=$(getent passwd "$INSTALL_USER" | cut -d: -f6)
else
    INSTALL_USER="$USER"
    INSTALL_HOME="$HOME"
fi

INSTALL_DIR="${INSTALL_HOME}/TRMM"

echo "[1/6] Updating system packages..."
sudo apt-get update -y
sudo apt-get install -y python3 python3-pip python3-venv git nginx curl unzip ufw openjdk-17-jdk-headless

echo "[2/6] Configuring repository at ${INSTALL_DIR}..."
if [ ! -d "${INSTALL_DIR}" ]; then
    echo "Cloning repository from GitHub..."
    git clone https://github.com/Samace-TechLab/trmm.git "${INSTALL_DIR}"
else
    echo "Repository directory already exists. Pulling latest updates..."
    cd "${INSTALL_DIR}"
    git pull origin main || true
fi

mkdir -p "${INSTALL_DIR}/generated_agents"
sudo chown -R "${INSTALL_USER}:${INSTALL_USER}" "${INSTALL_DIR}"

cd "${INSTALL_DIR}"

echo "[3/6] Setting up Python virtual environment..."
if [ ! -d "${INSTALL_DIR}/venv" ]; then
    python3 -m venv "${INSTALL_DIR}/venv"
fi

"${INSTALL_DIR}/venv/bin/pip" install --upgrade pip setuptools wheel
"${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"

echo "[4/6] Installing Systemd background daemon (trmm.service)..."
cat <<EOF | sudo tee /etc/systemd/system/trmm.service > /dev/null
[Unit]
Description=Tactical RMM Master Hub Server
After=network.target

[Service]
Type=simple
User=${INSTALL_USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/venv/bin/python ${INSTALL_DIR}/run_master.py --host 127.0.0.1 --port 8000 --no-reload
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable trmm.service
sudo systemctl restart trmm.service

echo "[5/6] Configuring Nginx reverse proxy with WebSocket support..."
cat << 'EOF' | sudo tee /etc/nginx/sites-available/trmm > /dev/null
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;

    client_max_body_size 250M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;

        # WebSocket headers
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Timeout settings for persistent long-lived agent WebSockets
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
        proxy_buffering off;
    }
}
EOF

# Enable site if not already enabled
if [ ! -f /etc/nginx/sites-enabled/trmm ]; then
    sudo rm -f /etc/nginx/sites-enabled/default
    sudo ln -s /etc/nginx/sites-available/trmm /etc/nginx/sites-enabled/trmm
fi

sudo nginx -t
sudo systemctl reload nginx

echo "[6/6] Checking Service Status..."
sleep 2
sudo systemctl status trmm.service --no-pager

PUBLIC_IP=$(curl -s http://checkip.amazonaws.com || curl -s https://ifconfig.me || echo "YOUR-SERVER-PUBLIC-IP")

echo "=================================================================="
echo "  [SUCCESS] Tactical RMM has been successfully deployed!"
echo "  Dashboard URL : http://${PUBLIC_IP}"
echo "  Admin Login   : Master All-Access Key: 0235059609567482"
echo ""
echo "  Optional SSL Setup (Once you point a custom domain):"
echo "    sudo apt-get install -y certbot python3-certbot-nginx"
echo "    sudo certbot --nginx -d yourdomain.com"
echo "=================================================================="

