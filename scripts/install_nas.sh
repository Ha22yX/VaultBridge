#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/vaultbridge}"
DATA_DIR="${DATA_DIR:-/storage/Files/服务器备份/147.189.128.208/.vaultbridge-data}"
PORT="${PORT:-8728}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required"
  exit 1
fi

if ! command -v git >/dev/null 2>&1; then
  echo "git is required"
  exit 1
fi

mkdir -p "$APP_DIR" "$DATA_DIR"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install "$APP_DIR"

SECRET_KEY="$("$APP_DIR/.venv/bin/python" -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"

cat > "$APP_DIR/.env" <<EOF
VAULTBRIDGE_HOST=0.0.0.0
VAULTBRIDGE_PORT=$PORT
VAULTBRIDGE_DATA_DIR=$DATA_DIR
BACKUP_SECRET_KEY=$SECRET_KEY
EOF

cat > /etc/systemd/system/vaultbridge.service <<EOF
[Unit]
Description=VaultBridge backup panel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/python -m app.main
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now vaultbridge

echo "VaultBridge is running on port $PORT"

