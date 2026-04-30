#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BASE_DIR/SH_discord_bot_split"

if [ ! -d .venv ]; then
  echo "[ERROR] Не найдено виртуальное окружение .venv. Сначала запусти: ./install.sh" >&2
  exit 1
fi

# shellcheck disable=SC1091
source .venv/bin/activate

exec python main.py
