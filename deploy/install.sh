#!/usr/bin/env bash
# Install MediaFinder as a systemd service on Debian/Ubuntu (a Proxmox LXC, a VM, a Pi...).
# No Docker needed.
#
#   install:   bash install.sh
#   update:    bash install.sh update
#   remove:    bash install.sh uninstall      (keeps your data in /var/lib/mediafinder)
#
# Options (environment variables):
#   PORT=8080   MEDIAFINDER_PASSWORD=...   BROWSE_ROOTS=/mnt,/media,/srv   REPO_URL=...   BRANCH=main
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/jthy10/MediaFinder.git}"
BRANCH="${BRANCH:-main}"
APP_DIR=/opt/mediafinder
DATA_DIR=/var/lib/mediafinder
ENV_FILE=/etc/mediafinder.env
UNIT=/etc/systemd/system/mediafinder.service
USER_NAME=mediafinder

[ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo bash install.sh)"; exit 1; }
cmd="${1:-install}"

fetch_code() {
  if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
    git -C "$APP_DIR" reset --quiet --hard "origin/$BRANCH"
  else
    git clone --quiet --depth 1 --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
  fi
}

install_deps() {
  [ -d "$APP_DIR/.venv" ] || python3 -m venv "$APP_DIR/.venv"
  "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
  "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
}

case "$cmd" in
  install)
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq python3 python3-venv git ca-certificates >/dev/null
    id "$USER_NAME" >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin "$USER_NAME"
    mkdir -p "$DATA_DIR" && chown "$USER_NAME":"$USER_NAME" "$DATA_DIR" && chmod 750 "$DATA_DIR"
    fetch_code
    install_deps
    if [ ! -f "$ENV_FILE" ]; then
      umask 077
      cat > "$ENV_FILE" <<EOF
HOST=0.0.0.0
PORT=${PORT:-8080}
DB_PATH=$DATA_DIR/mediafinder.db
BROWSE_ROOTS=${BROWSE_ROOTS:-/media,/mnt,/srv}
EOF
      [ -n "${MEDIAFINDER_PASSWORD:-}" ] && echo "MEDIAFINDER_PASSWORD=$MEDIAFINDER_PASSWORD" >> "$ENV_FILE"
    fi
    cat > "$UNIT" <<EOF
[Unit]
Description=MediaFinder
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
EnvironmentFile=$ENV_FILE
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/uvicorn mediafinder.app:app_factory --factory --host \${HOST} --port \${PORT}
Restart=on-failure
NoNewPrivileges=true
ProtectSystem=full
ReadWritePaths=$DATA_DIR

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now mediafinder >/dev/null
    ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    echo "MediaFinder is running: http://${ip:-<this-host>}:$(grep ^PORT= "$ENV_FILE" | cut -d= -f2)"
    echo "Open it, then set a password in Settings > Security."
    ;;
  update)
    fetch_code
    install_deps
    systemctl restart mediafinder
    echo "Updated and restarted."
    ;;
  uninstall)
    systemctl disable --now mediafinder 2>/dev/null || true
    rm -f "$UNIT" "$ENV_FILE"
    rm -rf "$APP_DIR"
    systemctl daemon-reload
    echo "Removed. Your data is still in $DATA_DIR (delete it manually if you want it gone)."
    ;;
  *)
    echo "usage: $0 [install|update|uninstall]"; exit 1 ;;
esac
