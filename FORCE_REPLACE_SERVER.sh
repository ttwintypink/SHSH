#!/usr/bin/env bash
set -euo pipefail

# Запускать из папки проекта SHSH на сервере.
# ВАЖНО: .env не трогается.

echo "[1/4] Обновляю git..."
git fetch origin main || true
git reset --hard origin/main || true

echo "[2/4] Удаляю Python cache..."
find . -type d -name "__pycache__" -prune -exec rm -rf {} + || true
find . -type f -name "*.pyc" -delete || true

echo "[3/4] Проверяю channel_protection.py..."
if grep -R "__dict__" -n SH_discord_bot_split/channel_protection.py; then
  echo "ОШИБКА: в channel_protection.py всё ещё есть __dict__ — файл старый."
  exit 1
else
  echo "OK: __dict__ не найден."
fi

echo "[4/4] Запусти/перезапусти бота:"
echo "  systemctl restart sh-discord-bot"
echo "или:"
echo "  pkill -f python || true && python3 main.py"
