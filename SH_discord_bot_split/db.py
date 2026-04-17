# db.py
import sqlite3
import time

from config import DB_PATH

# ==========================================================
#                      DB HELPERS
# ==========================================================


def db_init() -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS tickets ("
            "channel_id INTEGER PRIMARY KEY, "
            "opener_id INTEGER NOT NULL, "
            "created_at INTEGER NOT NULL"
            ");"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS prompts ("
            "channel_id INTEGER PRIMARY KEY, "
            "prompt_message_id INTEGER NOT NULL, "
            "created_at INTEGER NOT NULL"
            ");"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS private_setup ("
            "channel_id INTEGER PRIMARY KEY, "
            "message_id INTEGER NOT NULL, "
            "created_at INTEGER NOT NULL"
            ");"
        )
        # Пользователи, которых нельзя считать "автором тикета"
        con.execute(
            "CREATE TABLE IF NOT EXISTS ignored_users ("
            "user_id INTEGER PRIMARY KEY, "
            "added_by INTEGER NOT NULL, "
            "added_at INTEGER NOT NULL"
            ");"
        )
        # Логи инвайтов в приватку (аудит)
        con.execute(
            "CREATE TABLE IF NOT EXISTS invite_logs ("
            "invite_code TEXT PRIMARY KEY, "
            "user_id INTEGER NOT NULL, "
            "moderator_id INTEGER NOT NULL, "
            "channel_id INTEGER NOT NULL, "
            "created_at INTEGER NOT NULL, "
            "expires_at INTEGER NOT NULL"
            ");"
        )


def db_set_opener(channel_id: int, opener_id: int) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT INTO tickets(channel_id, opener_id, created_at) VALUES(?, ?, ?) "
            "ON CONFLICT(channel_id) DO UPDATE SET opener_id=excluded.opener_id, created_at=excluded.created_at;",
            (channel_id, opener_id, int(time.time())),
        )


def db_get_opener(channel_id: int) -> int | None:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT opener_id FROM tickets WHERE channel_id=?;",
            (channel_id,),
        ).fetchone()
    return int(row[0]) if row else None


def db_delete_ticket(channel_id: int) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute("DELETE FROM tickets WHERE channel_id=?;", (channel_id,))


def db_set_prompt(channel_id: int, message_id: int) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT INTO prompts(channel_id, prompt_message_id, created_at) VALUES(?, ?, ?) "
            "ON CONFLICT(channel_id) DO UPDATE SET prompt_message_id=excluded.prompt_message_id, created_at=excluded.created_at;",
            (channel_id, message_id, int(time.time())),
        )


def db_get_prompt(channel_id: int) -> int | None:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT prompt_message_id FROM prompts WHERE channel_id=?;",
            (channel_id,),
        ).fetchone()
    return int(row[0]) if row else None


def db_delete_prompt(channel_id: int) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute("DELETE FROM prompts WHERE channel_id=?;", (channel_id,))


def db_set_private_setup_message(channel_id: int, message_id: int) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT INTO private_setup(channel_id, message_id, created_at) VALUES(?, ?, ?) "
            "ON CONFLICT(channel_id) DO UPDATE SET message_id=excluded.message_id, created_at=excluded.created_at;",
            (channel_id, message_id, int(time.time())),
        )


def db_get_private_setup_message(channel_id: int) -> int | None:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT message_id FROM private_setup WHERE channel_id=?;",
            (channel_id,),
        ).fetchone()
    return int(row[0]) if row else None


def db_delete_private_setup_message(channel_id: int) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute("DELETE FROM private_setup WHERE channel_id=?;", (channel_id,))


# -------------------- IGNORE USERS --------------------


def db_add_ignored_user(user_id: int, added_by: int) -> None:
    """Добавляет user_id в ignored_users (если его там ещё нет)."""
    try:
        with sqlite3.connect(DB_PATH) as con:
            con.execute(
                "INSERT OR IGNORE INTO ignored_users(user_id, added_by, added_at) VALUES(?, ?, ?);",
                (user_id, added_by, int(time.time())),
            )
    except sqlite3.OperationalError:
        # таблица ещё не создана (на очень раннем старте)
        pass


def db_is_ignored_user(user_id: int) -> bool:
    try:
        with sqlite3.connect(DB_PATH) as con:
            row = con.execute(
                "SELECT 1 FROM ignored_users WHERE user_id=?;",
                (user_id,),
            ).fetchone()
        return row is not None
    except sqlite3.OperationalError:
        return False


def db_remove_ignored_user(user_id: int) -> bool:
    """Удаляет user_id из ignored_users. Возвращает True если реально было удалено."""
    try:
        with sqlite3.connect(DB_PATH) as con:
            cur = con.execute("DELETE FROM ignored_users WHERE user_id=?;", (user_id,))
            return (cur.rowcount or 0) > 0
    except sqlite3.OperationalError:
        return False


def db_list_ignored_users() -> list[int]:
    """Возвращает список user_id из ignored_users."""
    try:
        with sqlite3.connect(DB_PATH) as con:
            rows = con.execute("SELECT user_id FROM ignored_users ORDER BY added_at ASC;").fetchall()
        return [int(r[0]) for r in rows]
    except sqlite3.OperationalError:
        return []


# -------------------- INVITE LOGS --------------------


def db_log_invite(invite_code: str, user_id: int, moderator_id: int, channel_id: int, expires_at: int) -> None:
    """Логирует созданный инвайт в БД для аудита."""
    try:
        with sqlite3.connect(DB_PATH) as con:
            con.execute(
                "INSERT OR REPLACE INTO invite_logs(invite_code, user_id, moderator_id, channel_id, created_at, expires_at) "
                "VALUES(?, ?, ?, ?, ?, ?);",
                (invite_code, user_id, moderator_id, channel_id, int(time.time()), expires_at),
            )
    except sqlite3.OperationalError:
        pass
