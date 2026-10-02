#!/usr/bin/env bash
# Install voicebot as a systemd service on Ubuntu (runs 24/7, starts on boot).
# Must run as root. Installs from THIS checkout (no re-clone confusion).
#
#   sudo ./scripts/install-service.sh [--user voicebot] [--data-dir /var/lib/voicebot]
#                                     [--env-file .env] [--no-start]
#
# Reads Discord/provider secrets from --env-file (default: ./.env next to the
# repo) and writes them to /etc/voicebot/voicebot.env (mode 0640).
set -euo pipefail

SERVICE_USER="voicebot"
DATA_DIR="/var/lib/voicebot"
ENV_FILE=""
NO_START=0

while [ $# -gt 0 ]; do
  case "$1" in
    --user) SERVICE_USER="$2"; shift 2 ;;
    --data-dir) DATA_DIR="$2"; shift 2 ;;
    --env-file) ENV_FILE="$2"; shift 2 ;;
    --no-start) NO_START=1; shift ;;
    -h|--help)
      sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
done

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -z "$ENV_FILE" ]; then ENV_FILE="$REPO_DIR/.env"; fi
if [ "$(id -u)" -ne 0 ]; then echo "Run as root: sudo $0" >&2; exit 1; fi
if [ ! -f "$ENV_FILE" ]; then
  echo "Env file not found: $ENV_FILE" >&2
  echo "Run 'uv run voicebot setup' first, then re-run this script." >&2
  exit 1
fi
if [ ! -x "$REPO_DIR/.venv/bin/python" ]; then
  echo "No locked venv at $REPO_DIR/.venv — run ./scripts/bootstrap-ubuntu.sh first." >&2
  exit 1
fi

echo "==> Creating service user '$SERVICE_USER'"
id "$SERVICE_USER" >/dev/null 2>&1 || useradd --system --home-dir "$DATA_DIR" \
  --create-home --shell /usr/sbin/nologin "$SERVICE_USER"

echo "==> Preparing directories"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0700 "$DATA_DIR"
install -d -o root -g "$SERVICE_USER" -m 0750 /etc/voicebot
install -m 0640 -o root -g "$SERVICE_USER" "$ENV_FILE" /etc/voicebot/voicebot.env
chown -R "$SERVICE_USER:$SERVICE_USER" "$REPO_DIR/voice_logs" 2>/dev/null || true

echo "==> Rendering systemd unit"
UNIT_SRC="$REPO_DIR/deploy/voicebot.service"
UNIT_DST="/etc/systemd/system/voicebot.service"
sed -e "s|^User=.*|User=$SERVICE_USER|" \
    -e "s|^Group=.*|Group=$SERVICE_USER|" \
    -e "s|^WorkingDirectory=.*|WorkingDirectory=$REPO_DIR|" \
    -e "s|^Environment=DATA_ROOT=.*|Environment=DATA_ROOT=$DATA_DIR|" \
    -e "s|^ExecStart=.*|ExecStart=$REPO_DIR/.venv/bin/python $REPO_DIR/bot.py|" \
    -e "s|^ReadWritePaths=.*|ReadWritePaths=$DATA_DIR|" \
    "$UNIT_SRC" > "$UNIT_DST"
chmod 644 "$UNIT_DST"

systemctl daemon-reload
systemctl enable voicebot.service >/dev/null
if [ "$NO_START" -eq 0 ]; then
  echo "==> Starting voicebot.service"
  systemctl restart voicebot.service
  sleep 2
  systemctl --no-pager status voicebot.service | head -n 12 || true
else
  echo "==> Unit installed (not started: --no-start)"
fi

echo ""
echo "Useful commands:"
echo "  systemctl status voicebot"
echo "  journalctl -u voicebot -f"
echo "  sudo -u $SERVICE_USER $REPO_DIR/.venv/bin/python -m voicebot health"
