"""Конфигурация бота.

Цель: одинаково работать и на Windows, и на хостингах.

Приоритет источников токена:
  1) Переменные окружения (ENV) — то, что ты задаёшь в панели хоста.
  2) Файл .env (если существует) — БЕЗ python-dotenv, простым парсером key=value.
     Это нужно, чтобы локальный запуск «как раньше» не ломался.

Поддерживаемые имена переменных:
  DISCORD_TOKEN / TOKEN / BOT_TOKEN / DISCORD_BOT_TOKEN
"""

import os
from pathlib import Path

# ==========================================================
#                       CONFIG
# ==========================================================

def _clean_token(raw: str) -> str:
    """Чистим токен от пробелов/кавычек/переводов строки."""
    v = (raw or "").strip()
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        v = v[1:-1].strip()
    return v


def _first_env(*names: str) -> tuple[str | None, str | None]:
    for n in names:
        val = os.getenv(n)
        if val and val.strip():
            return _clean_token(val), n
    return None, None


def _parse_env_file(path: Path) -> dict[str, str]:
    """Мини-парсер .env: KEY=VALUE, без зависимостей."""
    data: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return data
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k = k.strip()
        v = _clean_token(v)
        if k:
            data[k] = v
    return data


def _first_from_envfile(*names: str) -> tuple[str | None, str | None]:
    # Ищем .env рядом с main.py и в корне проекта
    candidates = [
        Path(__file__).resolve().parent / ".env",
        Path(__file__).resolve().parent.parent / ".env",
        Path.cwd() / ".env",
    ]
    for env_path in candidates:
        if not env_path.exists():
            continue
        parsed = _parse_env_file(env_path)
        for n in names:
            val = parsed.get(n)
            if val and val.strip():
                return _clean_token(val), f"{env_path}::{n}"
    return None, None


TOKEN, _TOKEN_SRC = _first_env(
    "DISCORD_TOKEN",
    "TOKEN",
    "BOT_TOKEN",
    "DISCORD_BOT_TOKEN",
)

if not TOKEN:
    TOKEN, _TOKEN_SRC = _first_from_envfile(
        "DISCORD_TOKEN",
        "TOKEN",
        "BOT_TOKEN",
        "DISCORD_BOT_TOKEN",
    )

if not TOKEN:
    raise RuntimeError(
        "Токен бота не найден.\n"
        "Задай токен в панели хостинга через ENV (рекомендуется):\n"
        "  DISCORD_TOKEN  или  TOKEN  или  BOT_TOKEN  или  DISCORD_BOT_TOKEN\n"
        "Либо создай файл .env рядом с SH_discord_bot_split/main.py с одной строкой, например:\n"
        "  DISCORD_TOKEN=твой_токен\n"
    )

print(f"✅ TOKEN загружен из: {_TOKEN_SRC}")

# Категория, где Ticket Tool создаёт тикеты
TICKETS_CATEGORY_ID = 1371053315529637928

# Категория архива
ARCHIVE_CATEGORY_ID = 1287197183954784297

# Роли модераторов: имеют право принимать/отклонять + будут пинговаться
STAFF_ROLE_IDS: list[int] = [
    1300451930900009092,
    1364660029143253082,
    1371955005367779418,
    1320456192568459265,
    1320438406370824305,
    1364549372313993216,
    1349429359290876037,
]

# Роли, которые пингуем (оставляем как было раньше)
STAFF_PING_ROLE_IDS: list[int] = [
    1300451930900009092,
    1364660029143253082,
    1364549372313993216,
]

# (опционально) канал логов (0 = выключено)
LOG_CHANNEL_ID = 1466163549150773363

# Фраза-триггер (Ticket Tool пишет это при попытке закрыть тикет)
TRIGGER_PHRASE = "вы серьезно хотите закрыть данный тикет"

# Анти-спам: не чаще, чем раз в N секунд в одном канале
PROMPT_COOLDOWN_SECONDS = 30

# SQLite
DB_PATH = "tickets.db"

# Инвайт (permanent link)
INVITE_LINK = "https://discord.gg/shclan"

# Роли для результата заявки
ACCEPT_ADD_ROLE_ID = 1299444337658171422      # выдать при принятии
ACCEPT_REMOVE_ROLE_ID = 1315028367044513876   # забрать при принятии

ACCEPT_EXTRA_DM = """**Так же не забудьте поставить ник по форме в приватке, и добавить приписку в стим профиле!**
*Форма для стима: SH | nick*
*Форма для приватки: Ник | Имя*"""

WELCOME_MESSAGE = """**Шаблон для подачи заявки в клан:** 

```1) Возраст, имя, кол-во часов (на одном аккаунте, пиратки не считаются)
2) Ваши преимущества
3) Роль в клане
4) Играете ли вы час в день на юкн?
5) Был ли опыт в кланах? (если да то в каких)
6) Готовы вы ли вы покупать випки?
7) Профиль стим (ТРЕБУЕТСЯ открытый стим аккаунт)
8) Ваш часовой пояс
9) Сколько играете в день
10) Откуда узнали о нас?
11) Ваша характеристика пк (проц, видюха, сколько фпса)
12) Принимаете ли вы обоснованную критику в свою сторону?
13) Готовы пройти проверку?
14) 40 UKN walls - 45 R2 fc```

***Заполняйте свою заявку по форме выше!***"""

# -------------------- PRIVATKA SETUP --------------------
# Приватный сервер (гильдия) и канал, где будет сообщение с кнопкой формы
PRIVATE_GUILD_ID = 1454836789331230842
PRIVATE_SETUP_CHANNEL_ID = 1466168457757462741

# Роли в приватке:
# - роль, которую нужно снять после заполнения формы
# - роль, которую нужно выдать после заполнения формы
PRIVATE_REMOVE_ROLE_ID = 1466169062491951164
PRIVATE_ADD_ROLE_ID = 1454842309421170719

# -------------------- ROLE SYNC (PUBLIC <-> PRIVATE) --------------------
PUBLIC_GUILD_ID = 1287197183954784296
PRIVATE_GUILD_ID = 1454836789331230842

PUBLIC_ROLE_SH_ID = 1299444337658171422
PUBLIC_ROLE_FUN_SH_ID = 1315028367044513876
PRIVATE_ROLE_SH_ID = 1454842309421170719

# Текст сообщения с кнопкой (можешь поменять позже)
PRIVATE_SETUP_MESSAGE = (
    "**Приватка: установка ника**\n"
    "Нажми кнопку ниже, заполни форму — и я автоматически поставлю тебе ник по формату "
    "**`Ник в стиме | Настоящее имя`** и обновлю роли."
)

# -------------------- TICKET OPENER IGNORE --------------------
IGNORED_TICKET_OPENER_IDS: set[int] = {
    1069974638706315295,
    1166060811672883210,
    529349299948093440,
    831145749030895617,
    857271131580923914,
    491950199745413121,
}

IGNORED_TICKET_OPENER_ROLE_IDS: set[int] = {
    1300451930900009092,
    1364660029143253082,
    1371955005367779418,
    1320456192568459265,
    1320438406370824305,
    1364549372313993216,
}

IGNORE_ADD_ADMIN_ID = 1166060811672883210

# -------------------- PRIVATE INVITE SETTINGS --------------------
PRIVATE_INVITE_MAX_AGE_SECONDS = 86400
PRIVATE_INVITE_MAX_USES = 1
