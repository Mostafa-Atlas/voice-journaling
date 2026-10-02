#!/usr/bin/env bash
# Bootstrap a fresh Ubuntu 22.04/24.04 machine for voicebot (native, no Docker).
# Idempotent: safe to run twice. Run as your normal user (uses sudo for apt).
#
#   ./scripts/bootstrap-ubuntu.sh
#
# Then:  uv run voicebot setup  &&  uv run voicebot doctor
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

if [ "$(uname -s)" != "Linux" ]; then
  echo "This script targets Ubuntu Linux. You are on: $(uname -s)" >&2
  exit 1
fi

if [ "${EUID:-$(id -u)}" -eq 0 ]; then
  echo "Do not run bootstrap as root; it uses sudo only for apt." >&2
  exit 1
fi

echo "==> Installing system packages (curl, ca-certificates)"
sudo apt-get update -qq
sudo apt-get install -y -qq curl ca-certificates > /dev/null
echo "    done."

if ! command -v uv >/dev/null 2>&1; then
  echo "==> Installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
else
  echo "==> uv already installed ($(uv --version))"
fi
export PATH="$HOME/.local/bin:$PATH"

echo "==> Syncing Python environment (uv sync)"
uv sync
echo "==> Creating runtime directories"
mkdir -p logs voice_logs exports
chmod 700 logs voice_logs 2>/dev/null || true

echo ""
echo "Bootstrap complete. Next steps:"
echo "  1. uv run voicebot setup    # answer 3 questions, writes .env"
echo "  2. uv run voicebot doctor   # verify everything is green"
echo "  3. uv run python bot.py --dashboard"
echo ""
echo "For 24/7 operation see: sudo ./scripts/install-service.sh --help"
