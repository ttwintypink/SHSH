#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BASE_DIR/SH_discord_bot_split"

if ! command -v python3 >/dev/null 2>&1; then
  echo "[ERROR] python3 не найден. Установи Python 3.10+ (или 3.11/3.12)." >&2
  exit 1
fi

# Создаём виртуальное окружение
python3 -m venv .venv

# Активируем venv
# shellcheck disable=SC1091
source .venv/bin/activate

python -m pip install --upgrade pip wheel >/dev/null
pip install -r requirements.txt

echo "Готово ✅"
echo "ВАЖНО: токен берётся из ENV (переменных окружения), файл .env не используется."
echo "Пример:  export DISCORD_TOKEN=ВАШ_ТОКЕН"
echo "Запуск:  $BASE_DIR/run.sh"
