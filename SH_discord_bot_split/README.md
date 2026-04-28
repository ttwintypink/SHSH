# SH Discord Bot (split sources)

Это тот же бот, что и в монолитном `SH.py`, но разнесён по файлам для удобства.

## Запуск

См. также: **README_LINUX.md** (инструкции для Linux, скрипты запуска, systemd).

1) Установи зависимости:
```bash
pip install -r requirements.txt
```

2) Задай токен через переменную окружения (ENV).

Поддерживаются имена переменной (выбери одно):
- `DISCORD_TOKEN`
- `TOKEN`
- `BOT_TOKEN`
- `DISCORD_BOT_TOKEN`

Пример (Linux/macOS):
```bash
export DISCORD_TOKEN="ВАШ_ТОКЕН"
```

Пример (Windows PowerShell):
```powershell
$env:DISCORD_TOKEN="ВАШ_ТОКЕН"
```

3) Запусти:
```bash
python main.py
```

## Структура
- `config.py` — все константы/ID и загрузка токена
- `app.py` — intents + client + in-memory state (locks/cooldown)
- `db.py` — SQLite helpers
- `helpers.py` — утилиты (staff, trigger, ping)
- `logs.py` — логирование в канал
- `tickets.py` — логика тикетов (opener/roles/archive/prompt)
- `ui.py` — кнопки/модалки (принять/отклонить)
- `privatka.py` — форма приватки (ник + роли)
- `events.py` — обработчики событий
- `main.py` — точка входа
