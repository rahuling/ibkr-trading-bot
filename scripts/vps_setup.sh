#!/usr/bin/env bash
# One-time setup on Ubuntu 22.04 VPS.
# Run as root or sudo-capable user.
set -e

REPO_URL="https://github.com/rahuling/ibkr-trading-bot.git"
INSTALL_DIR="/opt/trading-bot"

echo "=== IBKR Trading Bot — VPS setup ==="

# 1. System packages
apt-get update -y
apt-get install -y python3 python3-pip python3-venv git curl ca-certificates

# 2. Docker
if ! command -v docker &>/dev/null; then
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -y
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
    systemctl enable docker
    systemctl start docker
    echo "Docker installed."
else
    echo "Docker already installed — skipping."
fi

# docker-compose standalone (for docker-compose.yml compatibility)
if ! command -v docker-compose &>/dev/null; then
    curl -SL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$(uname -m)" \
        -o /usr/local/bin/docker-compose
    chmod +x /usr/local/bin/docker-compose
fi

# 3. Clone or update repo
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "Repo exists — pulling latest..."
    git -C "$INSTALL_DIR" pull
else
    git clone "$REPO_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

# 4. Python virtual environment
python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt

# 5. Required directories
mkdir -p logs data ibc jts

# 6. Env file
if [ ! -f .env ]; then
    cp .env.example .env
    echo ""
    echo "IMPORTANT: Edit .env with your real credentials before starting:"
    echo "  nano $INSTALL_DIR/.env"
else
    echo ".env already exists — skipping."
fi

# 7. Systemd service for the Python bot
cp systemd/trading-bot.service /etc/systemd/system/trading-bot.service
systemctl daemon-reload
systemctl enable trading-bot.service

echo ""
echo "================================================================"
echo "Setup complete. Run these steps to finish:"
echo ""
echo "  1. Add credentials : nano $INSTALL_DIR/.env"
echo "  2. Pull IB Gateway : cd $INSTALL_DIR && docker-compose pull"
echo "  3. Start Gateway   : docker-compose up -d"
echo "  4. Start bot       : systemctl start trading-bot"
echo "  5. Check status    : systemctl status trading-bot"
echo "  6. Live logs       : journalctl -u trading-bot -f"
echo "================================================================"
