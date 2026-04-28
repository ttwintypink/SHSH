from __future__ import annotations

import asyncio
import sqlite3
import time
import email.utils
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
import aiohttp

from app import client, tree
from config import DB_PATH

# ==========================================================
#                 CLAN EVENT CHECKER CONFIG
# ==========================================================

MSK_TZ = ZoneInfo("Europe/Moscow")
EMBED_COLOR = 0xB58CFF  # мягко-фиолетовая полоска слева, Apollo-like

# Временные роли отметки
EVENT_NOT_VOTED_ROLE_ID = 1467625280687308950
EVENT_ACCEPTED_ROLE_ID = 1467625274148655329
EVENT_DECLINED_ROLE_ID = 1467625406508171364
EVENT_TENTATIVE_ROLE_ID = 1467625283694624768

EVENT_ROLE_IDS = (
    EVENT_NOT_VOTED_ROLE_ID,
    EVENT_ACCEPTED_ROLE_ID,
    EVENT_DECLINED_ROLE_ID,
    EVENT_TENTATIVE_ROLE_ID,
)

# Пользователи, которым не выдаём и не снимаем временные event-роли
EVENT_ROLE_SKIP_USER_IDS = {
    1069974638706315295,
    1105559182624694393,
}

# Автопинги роли "ещё не отметился"
REMINDER_OFFSETS_SECONDS = (24 * 3600, 12 * 3600, 6 * 3600, 3 * 3600, 1 * 3600)
REMINDER_DELETE_AFTER_SECONDS = 3600
EVENT_WORKER_INTERVAL_SECONDS = 10
MASS_ROLE_DELAY_SECONDS = 0.10
MEMBER_CHUNK_TIMEOUT_SECONDS = 8
MEMBER_FETCH_TIMEOUT_SECONDS = 45
PREPARE_HARD_TIMEOUT_SECONDS = 180


_TIME100_OFFSET_SECONDS = 0.0
_TIME100_LAST_SYNC = 0.0
_PUBLISHING_EVENTS: set[int] = set()
_PREPARING_EVENTS: set[int] = set()
_ROLE_JOB_TASKS: dict[int, asyncio.Task] = {}

def _now_ts() -> int:
    return int(time.time() + _TIME100_OFFSET_SECONDS)

async def _sync_time100(force: bool = False) -> None:
    global _TIME100_OFFSET_SECONDS, _TIME100_LAST_SYNC
    if not force and time.time() - _TIME100_LAST_SYNC < 300:
        return
    _TIME100_LAST_SYNC = time.time()
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get("https://time100.ru/", allow_redirects=True) as resp:
                date_header = resp.headers.get("Date")
        if not date_header:
            raise RuntimeError("time100.ru did not return Date header")
        remote_dt = email.utils.parsedate_to_datetime(date_header)
        if remote_dt.tzinfo is None:
            remote_dt = remote_dt.replace(tzinfo=timezone.utc)
        _TIME100_OFFSET_SECONDS = remote_dt.timestamp() - time.time()
        _log(f"[ClanEvent] time source synced via time100.ru offset={_TIME100_OFFSET_SECONDS:.2f}s")
    except Exception as e:
        _log(f"[ClanEvent] time100 sync failed, using host time: {type(e).__name__}: {e}")

DATE_FORMAT_HINT = "28.04.2026 18:00"


def _log(message: str) -> None:
    print(message, flush=True)


# ==========================================================
#                         DB
# ==========================================================

@dataclass(slots=True)
class EventRecord:
    message_id: int
    guild_id: int
    channel_id: int
    creator_id: int
    title: str
    description: str
    start_ts: int
    end_ts: int
    member_limit: int
    status: str
    cleaned_at: int | None


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def event_db_init() -> None:
    with _connect() as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS clan_events ("
            "message_id INTEGER PRIMARY KEY, "
            "guild_id INTEGER NOT NULL, "
            "channel_id INTEGER NOT NULL, "
            "creator_id INTEGER NOT NULL, "
            "title TEXT NOT NULL, "
            "description TEXT NOT NULL, "
            "start_ts INTEGER NOT NULL, "
            "end_ts INTEGER NOT NULL, "
            "member_limit INTEGER NOT NULL, "
            "status TEXT NOT NULL DEFAULT 'active', "
            "cleaned_at INTEGER"
            ");"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS clan_event_responses ("
            "message_id INTEGER NOT NULL, "
            "user_id INTEGER NOT NULL, "
            "status TEXT NOT NULL, "
            "updated_at INTEGER NOT NULL, "
            "PRIMARY KEY(message_id, user_id)"
            ");"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS clan_event_reminders ("
            "message_id INTEGER NOT NULL, "
            "offset_seconds INTEGER NOT NULL, "
            "sent_message_id INTEGER, "
            "sent_at INTEGER NOT NULL, "
            "PRIMARY KEY(message_id, offset_seconds)"
            ");"
        )

        # Мягкая миграция для старых баз: если таблица уже была создана в старой версии,
        # CREATE TABLE IF NOT EXISTS не добавит новые колонки сам.
        existing_cols = {row[1] for row in con.execute("PRAGMA table_info(clan_events);").fetchall()}
        if "status" not in existing_cols:
            con.execute("ALTER TABLE clan_events ADD COLUMN status TEXT NOT NULL DEFAULT 'active';")
        if "cleaned_at" not in existing_cols:
            con.execute("ALTER TABLE clan_events ADD COLUMN cleaned_at INTEGER;")


def _insert_event(rec: EventRecord) -> None:
    with _connect() as con:
        con.execute(
            "INSERT INTO clan_events(message_id, guild_id, channel_id, creator_id, title, description, start_ts, end_ts, member_limit, status, cleaned_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
            (
                rec.message_id,
                rec.guild_id,
                rec.channel_id,
                rec.creator_id,
                rec.title,
                rec.description,
                rec.start_ts,
                rec.end_ts,
                rec.member_limit,
                rec.status,
                rec.cleaned_at,
            ),
        )


def _update_event(rec: EventRecord) -> None:
    with _connect() as con:
        con.execute(
            "UPDATE clan_events SET title=?, description=?, start_ts=?, end_ts=?, member_limit=? WHERE message_id=?;",
            (rec.title, rec.description, rec.start_ts, rec.end_ts, rec.member_limit, rec.message_id),
        )


def _get_event(message_id: int) -> EventRecord | None:
    with _connect() as con:
        row = con.execute(
            "SELECT message_id, guild_id, channel_id, creator_id, title, description, start_ts, end_ts, member_limit, status, cleaned_at "
            "FROM clan_events WHERE message_id=?;",
            (message_id,),
        ).fetchone()
    if not row:
        return None
    return EventRecord(*row)


def _get_active_events() -> list[EventRecord]:
    with _connect() as con:
        rows = con.execute(
            "SELECT message_id, guild_id, channel_id, creator_id, title, description, start_ts, end_ts, member_limit, status, cleaned_at "
            "FROM clan_events WHERE status IN ('preparing', 'scheduled', 'active', 'role_error') ORDER BY start_ts ASC;"
        ).fetchall()
    return [EventRecord(*row) for row in rows]


def _has_active_event(guild_id: int) -> EventRecord | None:
    now = _now_ts()
    with _connect() as con:
        row = con.execute(
            "SELECT message_id, guild_id, channel_id, creator_id, title, description, start_ts, end_ts, member_limit, status, cleaned_at "
            "FROM clan_events WHERE guild_id=? AND status IN ('preparing', 'scheduled', 'active') AND end_ts>? ORDER BY start_ts ASC LIMIT 1;",
            (guild_id, now),
        ).fetchone()
    return EventRecord(*row) if row else None


def _set_event_status(message_id: int, status: str) -> None:
    with _connect() as con:
        con.execute("UPDATE clan_events SET status=? WHERE message_id=?;", (status, message_id))


def _update_event_message_id(old_message_id: int, new_message_id: int, *, status: str = "active") -> None:
    with _connect() as con:
        con.execute(
            "UPDATE clan_events SET message_id=?, status=? WHERE message_id=?;",
            (new_message_id, status, old_message_id),
        )
        con.execute(
            "UPDATE clan_event_responses SET message_id=? WHERE message_id=?;",
            (new_message_id, old_message_id),
        )
        con.execute(
            "UPDATE clan_event_reminders SET message_id=? WHERE message_id=?;",
            (new_message_id, old_message_id),
        )


def _cancel_guild_events(guild_id: int) -> int:
    with _connect() as con:
        cur = con.execute(
            "UPDATE clan_events SET status='deleted', cleaned_at=? WHERE guild_id=? AND status IN ('preparing', 'scheduled', 'active', 'role_error');",
            (_now_ts(), guild_id),
        )
        return int(cur.rowcount or 0)


def _new_scheduled_message_id() -> int:
    # Временный отрицательный ID нужен, пока событие ещё не опубликовано в Discord.
    return -int(time.time() * 1000)


def _mark_event_cleaned(message_id: int) -> None:
    with _connect() as con:
        con.execute(
            "UPDATE clan_events SET status='finished', cleaned_at=? WHERE message_id=?;",
            (_now_ts(), message_id),
        )


def _set_response(message_id: int, user_id: int, status: str) -> None:
    with _connect() as con:
        con.execute(
            "INSERT INTO clan_event_responses(message_id, user_id, status, updated_at) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(message_id, user_id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at;",
            (message_id, user_id, status, _now_ts()),
        )


def _get_responses(message_id: int) -> dict[int, str]:
    with _connect() as con:
        rows = con.execute(
            "SELECT user_id, status FROM clan_event_responses WHERE message_id=?;",
            (message_id,),
        ).fetchall()
    return {int(user_id): str(status) for user_id, status in rows}


def _reminder_was_sent(message_id: int, offset_seconds: int) -> bool:
    with _connect() as con:
        row = con.execute(
            "SELECT 1 FROM clan_event_reminders WHERE message_id=? AND offset_seconds=?;",
            (message_id, offset_seconds),
        ).fetchone()
    return row is not None


def _mark_reminder_sent(message_id: int, offset_seconds: int, sent_message_id: int | None) -> None:
    with _connect() as con:
        con.execute(
            "INSERT OR IGNORE INTO clan_event_reminders(message_id, offset_seconds, sent_message_id, sent_at) VALUES(?, ?, ?, ?);",
            (message_id, offset_seconds, sent_message_id, _now_ts()),
        )


# ==========================================================
#                    FORMAT / VALIDATION
# ==========================================================


def _parse_msk_datetime(raw: str) -> datetime:
    text = (raw or "").strip()
    try:
        dt = datetime.strptime(text, "%d.%m.%Y %H:%M")
    except ValueError as e:
        raise ValueError(f"Неверный формат времени. Пример: {DATE_FORMAT_HINT}") from e
    return dt.replace(tzinfo=MSK_TZ)


def _fmt_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts: list[str] = []
    if days:
        parts.append(f"{days} д.")
    if hours:
        parts.append(f"{hours} ч.")
    if minutes and not days:
        parts.append(f"{minutes} мин.")
    return " ".join(parts) if parts else "меньше минуты"


def _truncate_lines(lines: list[str], limit: int = 1024) -> str:
    if not lines:
        return "Пока пусто"
    out: list[str] = []
    total = 0
    for line in lines:
        add_len = len(line) + 1
        if total + add_len > limit - 20:
            out.append("…")
            break
        out.append(line)
        total += add_len
    return "\n".join(out)[:limit]


def _member_line(guild: discord.Guild, user_id: int) -> str:
    member = guild.get_member(user_id)
    if member:
        return member.display_name
    return f"<@{user_id}>"


def build_event_embed(guild: discord.Guild, rec: EventRecord) -> discord.Embed:
    responses = _get_responses(rec.message_id)
    accepted = [uid for uid, st in responses.items() if st == "accepted"]
    declined = [uid for uid, st in responses.items() if st == "declined"]
    tentative = [uid for uid, st in responses.items() if st == "tentative"]

    embed = discord.Embed(
        title=rec.title,
        description=rec.description,
        color=EMBED_COLOR,
    )

    now_ts = _now_ts()
    if now_ts < rec.start_ts:
        relative = f"через {_fmt_duration(rec.start_ts - now_ts)}"
    elif now_ts <= rec.end_ts:
        relative = "идёт сейчас"
    else:
        relative = "завершено"

    embed.add_field(
        name="Time",
        value=(
            f"<t:{rec.start_ts}:F> — <t:{rec.end_ts}:t> **МСК**\n"
            f"🕒 {relative}"
        ),
        inline=False,
    )

    embed.add_field(
        name=f"✅ Accepted ({len(accepted)}/{rec.member_limit})",
        value=_truncate_lines([_member_line(guild, uid) for uid in accepted]),
        inline=True,
    )
    embed.add_field(
        name=f"❌ Declined ({len(declined)})",
        value=_truncate_lines([_member_line(guild, uid) for uid in declined]),
        inline=True,
    )
    embed.add_field(
        name=f"❔ Tentative ({len(tentative)})",
        value=_truncate_lines([_member_line(guild, uid) for uid in tentative]),
        inline=True,
    )

    creator = guild.get_member(rec.creator_id)
    creator_name = creator.display_name if creator else f"ID {rec.creator_id}"
    embed.set_footer(text=f"Created by {creator_name}")
    return embed


# ==========================================================
#                         ROLES
# ==========================================================


def _get_event_roles(guild: discord.Guild) -> dict[str, discord.Role] | None:
    roles = {
        "not_voted": guild.get_role(EVENT_NOT_VOTED_ROLE_ID),
        "accepted": guild.get_role(EVENT_ACCEPTED_ROLE_ID),
        "declined": guild.get_role(EVENT_DECLINED_ROLE_ID),
        "tentative": guild.get_role(EVENT_TENTATIVE_ROLE_ID),
    }
    if any(role is None for role in roles.values()):
        return None
    return roles  # type: ignore[return-value]


async def _clear_event_roles(member: discord.Member, roles: dict[str, discord.Role], *, reason: str) -> None:
    remove = [role for role in roles.values() if role in member.roles]
    if remove:
        await member.remove_roles(*remove, reason=reason)


async def _apply_response_role(member: discord.Member, status: str, *, reason: str) -> None:
    if member.id in EVENT_ROLE_SKIP_USER_IDS:
        return
    roles = _get_event_roles(member.guild)
    if roles is None:
        raise RuntimeError("Одна или несколько event-ролей не найдены на сервере.")
    await _clear_event_roles(member, roles, reason=reason)
    target = roles[status]
    if target not in member.roles:
        await member.add_roles(target, reason=reason)


def _status_label(status: str) -> str:
    return {
        "preparing": "выдаёт роли",
        "scheduled": "запланировано",
        "active": "опубликовано",
        "role_error": "ошибка выдачи ролей",
        "deleted": "удалено",
        "finished": "завершено",
    }.get(status, status)


def _get_bot_member(guild: discord.Guild) -> discord.Member | None:
    if client.user is None:
        return None
    member = guild.get_member(client.user.id)
    if member is not None:
        return member
    me = getattr(guild, "me", None)
    return me if isinstance(me, discord.Member) else None


def _role_permission_report(guild: discord.Guild) -> tuple[bool, list[str]]:
    problems: list[str] = []
    bot_member = _get_bot_member(guild)
    if bot_member is None:
        problems.append("бот не найден как участник сервера")
        return False, problems
    if not bot_member.guild_permissions.manage_roles:
        problems.append("у бота нет права Manage Roles / Управление ролями")
    roles = _get_event_roles(guild)
    if roles is None:
        missing = [str(rid) for rid in EVENT_ROLE_IDS if guild.get_role(rid) is None]
        problems.append("не найдены event-роли: " + ", ".join(missing))
        return False, problems
    for name, role in roles.items():
        if role >= bot_member.top_role:
            problems.append(f"роль {role.name} ({role.id}) стоит выше/на уровне роли бота")
    return not problems, problems


async def _ensure_role_permissions(guild: discord.Guild) -> None:
    ok, problems = _role_permission_report(guild)
    if not ok:
        raise RuntimeError("; ".join(problems))


async def _load_human_members(guild: discord.Guild, *, context: str) -> list[discord.Member]:
    """Надёжно получаем людей сервера. Не зависаем навсегда на guild.chunk."""
    expected = int(guild.member_count or 0)
    by_id: dict[int, discord.Member] = {}

    def add_cached() -> None:
        for m in guild.members:
            if not m.bot and m.id not in EVENT_ROLE_SKIP_USER_IDS:
                by_id[m.id] = m

    add_cached()
    _log(f"[ClanEvent] members load start context={context} guild={guild.id} cached={len(by_id)} expected={expected}")

    try:
        await asyncio.wait_for(guild.chunk(cache=True), timeout=MEMBER_CHUNK_TIMEOUT_SECONDS)
        add_cached()
        _log(f"[ClanEvent] members chunk ok context={context} cached={len(by_id)} expected={expected}")
    except asyncio.TimeoutError:
        _log(f"[ClanEvent] members chunk timeout context={context}; trying REST fallback")
    except discord.PrivilegedIntentsRequired as e:
        _log(f"[ClanEvent] members chunk privileged intent error context={context}: {e}")
    except Exception as e:
        _log(f"[ClanEvent] members chunk warning context={context}: {type(e).__name__}: {e}")

    need_rest = False
    if expected <= 0:
        need_rest = len(by_id) == 0
    else:
        need_rest = len(by_id) < max(1, int(expected * 0.50))

    if need_rest:
        async def collect() -> list[discord.Member]:
            out: list[discord.Member] = []
            async for m in guild.fetch_members(limit=None):
                if not m.bot and m.id not in EVENT_ROLE_SKIP_USER_IDS:
                    out.append(m)
            return out

        try:
            fetched = await asyncio.wait_for(collect(), timeout=MEMBER_FETCH_TIMEOUT_SECONDS)
            for m in fetched:
                by_id[m.id] = m
            _log(f"[ClanEvent] members REST ok context={context} fetched={len(fetched)} total={len(by_id)} expected={expected}")
        except asyncio.TimeoutError:
            _log(f"[ClanEvent] members REST timeout context={context}; using cached={len(by_id)}")
        except discord.PrivilegedIntentsRequired as e:
            _log(f"[ClanEvent] members REST privileged intent error context={context}: {e}")
        except discord.Forbidden as e:
            _log(f"[ClanEvent] members REST forbidden context={context}: {e}")
        except Exception as e:
            _log(f"[ClanEvent] members REST warning context={context}: {type(e).__name__}: {e}")

    members = list(by_id.values())
    members.sort(key=lambda m: m.id)
    _log(f"[ClanEvent] members load done context={context} humans={len(members)} expected={expected}")
    return members


async def assign_not_voted_to_humans(guild: discord.Guild) -> tuple[int, int]:
    await _ensure_role_permissions(guild)
    roles = _get_event_roles(guild)
    if roles is None:
        raise RuntimeError("Одна или несколько event-ролей не найдены на сервере.")

    not_voted = roles["not_voted"]
    members = await _load_human_members(guild, context="assign_not_voted")
    if not members:
        raise RuntimeError(
            "бот не смог загрузить участников сервера. Проверь SERVER MEMBERS INTENT в Discord Developer Portal."
        )

    changed = 0
    failed = 0
    _log(f"[ClanEvent] assign_not_voted start guild={guild.id} humans={len(members)}")

    for idx, member in enumerate(members, start=1):
        try:
            remove = [role for role in roles.values() if role in member.roles and role != not_voted]
            if remove:
                await asyncio.wait_for(
                    member.remove_roles(*remove, reason="[Event] reset temporary event roles"),
                    timeout=20,
                )
            if not_voted not in member.roles:
                await asyncio.wait_for(
                    member.add_roles(not_voted, reason="[Event] event created: waiting for response"),
                    timeout=20,
                )
                changed += 1
        except (asyncio.TimeoutError, discord.Forbidden, discord.HTTPException) as e:
            failed += 1
            _log(f"[ClanEvent] assign role failed member={member.id}: {type(e).__name__}: {e}")
        except Exception as e:
            failed += 1
            _log(f"[ClanEvent] assign role unexpected member={member.id}: {type(e).__name__}: {e}")
        if idx % 10 == 0 or idx == len(members):
            _log(f"[ClanEvent] assign_not_voted progress guild={guild.id} {idx}/{len(members)} changed={changed} failed={failed}")
        await asyncio.sleep(MASS_ROLE_DELAY_SECONDS)

    _log(f"[ClanEvent] assign_not_voted done guild={guild.id} changed={changed} failed={failed} humans={len(members)}")
    return changed, failed



async def verify_not_voted_roles(guild: discord.Guild) -> tuple[int, int]:
    """Проверяет, что всем подходящим людям выдана роль "ещё не отметился".

    Возвращает: (eligible_count, missing_count).
    """
    roles = _get_event_roles(guild)
    if roles is None:
        raise RuntimeError("Одна или несколько event-ролей не найдены на сервере.")

    not_voted = roles["not_voted"]
    members = await _load_human_members(guild, context="verify_not_voted")
    eligible = len(members)
    missing = sum(1 for member in members if not_voted not in member.roles)
    _log(f"[ClanEvent] verify_not_voted guild={guild.id} eligible={eligible} missing={missing}")
    return eligible, missing

async def cleanup_event_roles(guild: discord.Guild) -> tuple[int, int]:
    """Снимает event-роли надёжно: сначала через role.members, потом через полный список людей."""
    await _ensure_role_permissions(guild)
    roles = _get_event_roles(guild)
    if roles is None:
        raise RuntimeError("Одна или несколько event-ролей не найдены на сервере.")

    event_roles = tuple(roles.values())
    candidates: dict[int, discord.Member] = {}

    for role in event_roles:
        for member in getattr(role, "members", []):
            if isinstance(member, discord.Member):
                candidates[member.id] = member

    members = await _load_human_members(guild, context="clear_roles")
    for member in members:
        if any(role in member.roles for role in event_roles):
            candidates[member.id] = member

    changed = 0
    failed = 0
    _log(f"[ClanEvent] clear_roles start guild={guild.id} candidates={len(candidates)}")
    for idx, member in enumerate(list(candidates.values()), start=1):
        if member.bot or member.id in EVENT_ROLE_SKIP_USER_IDS:
            continue
        remove = [role for role in event_roles if role in member.roles]
        if not remove:
            continue
        try:
            await asyncio.wait_for(
                member.remove_roles(*remove, reason="[Event] cleanup temporary roles"),
                timeout=20,
            )
            changed += 1
        except (asyncio.TimeoutError, discord.Forbidden, discord.HTTPException) as e:
            failed += 1
            _log(f"[ClanEvent] clear role failed member={member.id}: {type(e).__name__}: {e}")
        except Exception as e:
            failed += 1
            _log(f"[ClanEvent] clear role unexpected member={member.id}: {type(e).__name__}: {e}")
        if idx % 10 == 0 or idx == len(candidates):
            _log(f"[ClanEvent] clear_roles progress guild={guild.id} {idx}/{len(candidates)} changed={changed} failed={failed}")
        await asyncio.sleep(MASS_ROLE_DELAY_SECONDS)
    _log(f"[ClanEvent] clear_roles finished guild={guild.id} changed={changed} failed={failed}")
    return changed, failed

async def _prepare_event_core(message_id: int) -> None:
    rec = _get_event(message_id)
    if rec is None or rec.status != "preparing":
        return

    guild = client.get_guild(rec.guild_id)
    if guild is None:
        _log(f"[ClanEvent] prepare failed id={message_id}: guild not in cache")
        return

    try:
        changed, failed = await assign_not_voted_to_humans(guild)
        eligible, missing = await verify_not_voted_roles(guild)
    except Exception as e:
        _set_event_status(message_id, "role_error")
        _log(f"[ClanEvent] prepare fatal id={message_id}: {type(e).__name__}: {e}")
        try:
            channel = guild.get_channel(rec.channel_id) or await client.fetch_channel(rec.channel_id)
            if isinstance(channel, discord.TextChannel):
                await channel.send(
                    f"⚠️ Не смог подготовить событие `{rec.title}`: `{type(e).__name__}: {e}`\n"
                    f"Проверь `/event diagnose` и права роли бота."
                )
        except Exception:
            pass
        return

    if eligible > 0 and changed == 0 and missing >= eligible:
        _set_event_status(message_id, "role_error")
        _log(
            f"[ClanEvent] prepare cancelled id={message_id}: no roles applied; "
            f"eligible={eligible} changed={changed} failed={failed} missing={missing}"
        )
        return

    _set_event_status(message_id, "scheduled")
    _log(
        f"[ClanEvent] event roles ready id={message_id} eligible={eligible} "
        f"changed={changed} failed={failed} missing={missing}; status=scheduled"
    )

    rec = _get_event(message_id)
    if rec and rec.status == "scheduled":
        if _now_ts() >= rec.start_ts:
            await publish_scheduled_event(rec)
        else:
            _schedule_publish_job(message_id)


async def _prepare_event_after_create(message_id: int) -> None:
    """Форма -> роли -> проверка -> scheduled/publish. Не даём событию зависнуть."""
    if message_id in _PREPARING_EVENTS:
        return
    _PREPARING_EVENTS.add(message_id)
    try:
        await asyncio.wait_for(_prepare_event_core(message_id), timeout=PREPARE_HARD_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        _set_event_status(message_id, "role_error")
        _log(f"[ClanEvent] prepare hard timeout id={message_id} after {PREPARE_HARD_TIMEOUT_SECONDS}s")
    except Exception as e:
        _set_event_status(message_id, "role_error")
        _log(f"[ClanEvent] prepare task crashed id={message_id}: {type(e).__name__}: {e}")
    finally:
        _PREPARING_EVENTS.discard(message_id)
        _ROLE_JOB_TASKS.pop(message_id, None)

def _schedule_prepare_job(message_id: int) -> None:
    task = _ROLE_JOB_TASKS.get(message_id)
    if task is not None and not task.done():
        return
    _ROLE_JOB_TASKS[message_id] = client.loop.create_task(_prepare_event_after_create(message_id))


def _schedule_publish_job(message_id: int) -> None:
    client.loop.create_task(_publish_when_due(message_id))


def _scheduled_temp_id_age_seconds(message_id: int) -> float | None:
    if message_id >= 0:
        return None
    try:
        created_ms = abs(int(message_id))
        return max(0.0, time.time() - (created_ms / 1000.0))
    except Exception:
        return None


# ==========================================================
#                       UI / MODALS
# ==========================================================


def _is_event_manager(interaction: discord.Interaction, rec: EventRecord) -> bool:
    user = interaction.user
    if user.id == rec.creator_id:
        return True
    return isinstance(user, discord.Member) and user.guild_permissions.manage_guild


class EventCreateModal(discord.ui.Modal, title="Создание события"):
    event_title = discord.ui.TextInput(
        label="Название события",
        placeholder="Напишите в данное поле как будет называться событие",
        min_length=3,
        max_length=80,
        required=True,
    )
    description = discord.ui.TextInput(
        label="Описание события",
        placeholder="Напишите в данное поле описание к событию",
        style=discord.TextStyle.paragraph,
        min_length=5,
        max_length=700,
        required=True,
    )
    start_time = discord.ui.TextInput(
        label="Когда опубликовать событие?",
        placeholder="Пример: 28.04.2026 10:10",
        min_length=16,
        max_length=16,
        required=True,
    )
    end_time = discord.ui.TextInput(
        label="Когда закрыть отметки?",
        placeholder="Пример: 28.04.2026 10:10",
        min_length=16,
        max_length=16,
        required=True,
    )
    member_limit = discord.ui.TextInput(
        label="Лимит участников",
        placeholder="1-999",
        min_length=1,
        max_length=3,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("❌ Команда работает только на сервере в текстовом канале.", ephemeral=True)
            return

        if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("❌ Нужны права Manage Server / Управление сервером.", ephemeral=True)
            return

        existing = _has_active_event(interaction.guild.id)
        if existing:
            await interaction.response.send_message(
                f"❌ Уже есть активное событие: `{existing.title}`. Сначала заверши/удали его, потому что временные роли общие для одного события.",
                ephemeral=True,
            )
            return

        try:
            title = str(self.event_title.value).strip()
            desc = str(self.description.value).strip()
            start_dt = _parse_msk_datetime(str(self.start_time.value))
            end_dt = _parse_msk_datetime(str(self.end_time.value))
            limit = int(str(self.member_limit.value).strip())
            if not (1 <= limit <= 999):
                raise ValueError("Лимит должен быть числом от 1 до 999.")
            now = datetime.now(tz=MSK_TZ)
            # Разрешаем текущую минуту: пока бот выдаёт роли, время публикации может уже наступить.
            if start_dt.timestamp() < _now_ts() - 90:
                raise ValueError("Событие нельзя создать в прошлом.")
            if end_dt <= start_dt:
                raise ValueError("Поле 'Когда закрыть отметки?' должно быть позже поля 'Когда опубликовать событие?'. Например: публикация 28.04.2026 18:00, закрытие 28.04.2026 20:00.")
            if not title or not desc:
                raise ValueError("Название и описание не могут быть пустыми.")
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        rec = EventRecord(
            message_id=_new_scheduled_message_id(),
            guild_id=interaction.guild.id,
            channel_id=interaction.channel.id,
            creator_id=interaction.user.id,
            title=title,
            description=desc,
            start_ts=int(start_dt.timestamp()),
            end_ts=int(end_dt.timestamp()),
            member_limit=limit,
            status="preparing",
            cleaned_at=None,
        )
        _insert_event(rec)
        _log(f"[ClanEvent] preparing event id={rec.message_id} guild={rec.guild_id} channel={rec.channel_id} publish_ts={rec.start_ts} close_ts={rec.end_ts}")
        _schedule_prepare_job(rec.message_id)

        await interaction.followup.send(
            "✅ Событие принято. Сейчас выдаю всем людям роль `ещё не отметился`, "
            "после проверки опубликую событие по времени. Проверить статус можно через `/event list`.",
            ephemeral=True,
        )

class EventEditModal(discord.ui.Modal, title="Редактирование события"):
    def __init__(self, rec: EventRecord):
        super().__init__()
        self.rec = rec

        self.event_title = discord.ui.TextInput(
            label="Название события",
            placeholder="Напишите в данное поле как будет называться событие",
            default=rec.title,
            min_length=3,
            max_length=80,
            required=True,
        )
        self.description = discord.ui.TextInput(
            label="Описание события",
            placeholder="Напишите в данное поле описание к событию",
            default=rec.description,
            style=discord.TextStyle.paragraph,
            min_length=5,
            max_length=700,
            required=True,
        )
        start_dt = datetime.fromtimestamp(rec.start_ts, tz=MSK_TZ).strftime("%d.%m.%Y %H:%M")
        end_dt = datetime.fromtimestamp(rec.end_ts, tz=MSK_TZ).strftime("%d.%m.%Y %H:%M")
        self.start_time = discord.ui.TextInput(
            label="Когда опубликовать событие?",
            placeholder="Пример: 28.04.2026 10:10",
            default=start_dt,
            min_length=16,
            max_length=16,
            required=True,
        )
        self.end_time = discord.ui.TextInput(
            label="Когда закрыть отметки?",
            placeholder="Пример: 28.04.2026 10:10",
            default=end_dt,
            min_length=16,
            max_length=16,
            required=True,
        )
        self.member_limit = discord.ui.TextInput(
            label="Лимит участников",
            placeholder="1-999",
            default=str(rec.member_limit),
            min_length=1,
            max_length=3,
            required=True,
        )

        self.add_item(self.event_title)
        self.add_item(self.description)
        self.add_item(self.start_time)
        self.add_item(self.end_time)
        self.add_item(self.member_limit)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("❌ Сервер не найден.", ephemeral=True)
            return
        rec = _get_event(self.rec.message_id)
        if rec is None or rec.status != "active":
            await interaction.response.send_message("❌ Событие уже не активно или не найдено.", ephemeral=True)
            return
        if not _is_event_manager(interaction, rec):
            await interaction.response.send_message("❌ Редактировать может только создатель события или админ.", ephemeral=True)
            return

        try:
            title = str(self.event_title.value).strip()
            desc = str(self.description.value).strip()
            start_dt = _parse_msk_datetime(str(self.start_time.value))
            end_dt = _parse_msk_datetime(str(self.end_time.value))
            limit = int(str(self.member_limit.value).strip())
            if not (1 <= limit <= 999):
                raise ValueError("Лимит должен быть числом от 1 до 999.")
            if end_dt <= start_dt:
                raise ValueError("Поле 'Когда закрыть отметки?' должно быть позже поля 'Когда опубликовать событие?'. Например: публикация 28.04.2026 18:00, закрытие 28.04.2026 20:00.")
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return

        rec.title = title
        rec.description = desc
        rec.start_ts = int(start_dt.timestamp())
        rec.end_ts = int(end_dt.timestamp())
        rec.member_limit = limit
        _update_event(rec)

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            channel = interaction.guild.get_channel(rec.channel_id) or await interaction.client.fetch_channel(rec.channel_id)
            if isinstance(channel, discord.TextChannel):
                msg = await channel.fetch_message(rec.message_id)
                await msg.edit(embed=build_event_embed(interaction.guild, rec), view=EventView())
        except Exception:
            pass
        await interaction.followup.send("✅ Событие обновлено.", ephemeral=True)


class EventView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _respond(self, interaction: discord.Interaction, status: str) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("❌ Это работает только на сервере.", ephemeral=True)
            return
        if interaction.user.bot:
            await interaction.response.send_message("❌ Боты не участвуют в отметках.", ephemeral=True)
            return
        if not interaction.message:
            await interaction.response.send_message("❌ Не удалось определить сообщение события.", ephemeral=True)
            return
        rec = _get_event(interaction.message.id)
        if rec is None or rec.status != "active":
            await interaction.response.send_message("❌ Событие не найдено или уже завершено.", ephemeral=True)
            return

        # Лимит проверяем только для ✅.
        if status == "accepted":
            responses = _get_responses(rec.message_id)
            accepted_count = sum(1 for s in responses.values() if s == "accepted")
            old_status = responses.get(interaction.user.id)
            if old_status != "accepted" and accepted_count >= rec.member_limit:
                await interaction.response.send_message("❌ Лимит участников уже заполнен.", ephemeral=True)
                return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await _apply_response_role(interaction.user, status, reason=f"[Event] user response: {status}")
        except Exception as e:
            await interaction.followup.send(
                f"❌ Не смог изменить временную роль. Проверь права бота.\nОшибка: `{type(e).__name__}: {e}`",
                ephemeral=True,
            )
            return

        _set_response(rec.message_id, interaction.user.id, status)
        try:
            await interaction.message.edit(embed=build_event_embed(interaction.guild, rec), view=EventView())
        except discord.HTTPException:
            pass

        status_text = {
            "accepted": "✅ Ты отметил: буду.",
            "declined": "❌ Ты отметил: не буду.",
            "tentative": "❔ Ты отметил: возможно.",
        }[status]
        await interaction.followup.send(status_text, ephemeral=True)

    @discord.ui.button(emoji="✅", style=discord.ButtonStyle.success, custom_id="clan_event:accepted")
    async def accepted(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._respond(interaction, "accepted")

    @discord.ui.button(emoji="❌", style=discord.ButtonStyle.danger, custom_id="clan_event:declined")
    async def declined(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._respond(interaction, "declined")

    @discord.ui.button(emoji="❔", style=discord.ButtonStyle.primary, custom_id="clan_event:tentative")
    async def tentative(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._respond(interaction, "tentative")

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.primary, custom_id="clan_event:edit")
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.message:
            await interaction.response.send_message("❌ Не удалось определить событие.", ephemeral=True)
            return
        rec = _get_event(interaction.message.id)
        if rec is None or rec.status != "active":
            await interaction.response.send_message("❌ Событие не найдено или уже завершено.", ephemeral=True)
            return
        if not _is_event_manager(interaction, rec):
            await interaction.response.send_message("❌ Редактировать может только создатель события или админ.", ephemeral=True)
            return
        await interaction.response.send_modal(EventEditModal(rec))

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, custom_id="clan_event:delete")
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not interaction.message:
            await interaction.response.send_message("❌ Не удалось определить событие.", ephemeral=True)
            return
        rec = _get_event(interaction.message.id)
        if rec is None:
            await interaction.response.send_message("❌ Событие не найдено.", ephemeral=True)
            return
        if not _is_event_manager(interaction, rec):
            await interaction.response.send_message("❌ Удалить может только создатель события или админ.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            changed, failed = await cleanup_event_roles(interaction.guild)
        except Exception as e:
            await interaction.followup.send(f"❌ Не смог очистить временные роли: `{type(e).__name__}: {e}`", ephemeral=True)
            return
        _set_event_status(rec.message_id, "deleted")
        try:
            await interaction.message.delete()
        except discord.HTTPException:
            pass
        text = f"✅ Событие удалено. Временные роли сняты у {changed} пользователей."
        if failed:
            text += f" Ошибок: {failed}."
        await interaction.followup.send(text, ephemeral=True)


# ==========================================================
#                    SLASH COMMANDS
# ==========================================================


event_group = app_commands.Group(name="event", description="Клановые события и отметки участников")


@event_group.command(name="create", description="Создать событие с отметками ✅ ❌ ❔")
async def event_create(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("❌ Нужны права Manage Server / Управление сервером.", ephemeral=True)
        return
    await interaction.response.send_modal(EventCreateModal())


@event_group.command(name="clear_roles", description="Снять все временные event-роли со всех людей")
async def event_clear_roles(interaction: discord.Interaction):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("❌ Команда работает только на сервере.", ephemeral=True)
        return
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("❌ Нужны права Manage Server / Управление сервером.", ephemeral=True)
        return
    guild = interaction.guild
    cancelled = _cancel_guild_events(guild.id)
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        changed, failed = await cleanup_event_roles(guild)
        _log(f"[ClanEvent] clear_roles done guild={guild.id} changed={changed} failed={failed} cancelled={cancelled}")
        text = f"✅ Готово. Остановлено событий: {cancelled}. Временные event-роли сняты у {changed} пользователей."
        if failed:
            text += f" Ошибок снятия: {failed}."
        await interaction.followup.send(text, ephemeral=True)
    except Exception as e:
        _log(f"[ClanEvent] clear_roles failed guild={guild.id}: {type(e).__name__}: {e}")
        await interaction.followup.send(
            f"❌ Не смог снять временные event-роли: `{type(e).__name__}: {e}`",
            ephemeral=True,
        )

@event_group.command(name="diagnose", description="Проверить права бота, роли и загрузку участников")
async def event_diagnose(interaction: discord.Interaction):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("❌ Команда работает только на сервере.", ephemeral=True)
        return
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("❌ Нужны права Manage Server / Управление сервером.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    bot_member = _get_bot_member(guild)
    ok, problems = _role_permission_report(guild)
    roles = _get_event_roles(guild)
    try:
        members = await _load_human_members(guild, context="diagnose")
        members_info = f"людей загружено: {len(members)} / member_count={guild.member_count}"
    except Exception as e:
        members_info = f"ошибка загрузки людей: {type(e).__name__}: {e}"

    lines = [
        "**Диагностика Event Manager**",
        f"Бот: {bot_member.mention if bot_member else 'не найден'}",
        f"Manage Roles: {'✅' if bot_member and bot_member.guild_permissions.manage_roles else '❌'}",
        f"Top role бота: {bot_member.top_role.name if bot_member else 'не найден'}",
        f"Роли найдены: {'✅' if roles else '❌'}",
        f"Иерархия/права: {'✅ OK' if ok else '❌ ' + '; '.join(problems)}",
        f"Участники: {members_info}",
        f"Время now_ts: <t:{_now_ts()}:F>",
    ]
    if roles:
        for key, role in roles.items():
            lines.append(f"{key}: {role.mention} id={role.id} members_cache={len(getattr(role, 'members', []))}")
    await interaction.followup.send("\n".join(lines)[:1900], ephemeral=True)


@event_group.command(name="list", description="Показать активные события")
async def event_list(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("❌ Команда работает только на сервере.", ephemeral=True)
        return
    events = [e for e in _get_active_events() if e.guild_id == interaction.guild.id]
    for e in list(events):
        if e.status == "preparing" and e.message_id not in _PREPARING_EVENTS:
            age = _scheduled_temp_id_age_seconds(e.message_id)
            _schedule_prepare_job(e.message_id)
            _log(f"[ClanEvent] prepare job resumed from /event list id={e.message_id} age={age}")
        if e.status == "scheduled" and _now_ts() >= e.start_ts:
            try:
                await publish_scheduled_event(e)
            except Exception as ex:
                _log(f"[ClanEvent] lazy publish failed event={e.message_id}: {type(ex).__name__}: {ex}")
    events = [e for e in _get_active_events() if e.guild_id == interaction.guild.id]
    if not events:
        await interaction.response.send_message("Активных событий нет.", ephemeral=True)
        return
    def _line(e: EventRecord) -> str:
        state = _status_label(e.status)
        return f"• **{e.title}** — {state}, публикация <t:{e.start_ts}:R>, закрытие <t:{e.end_ts}:R> | канал <#{e.channel_id}>"

    lines = [_line(e) for e in events[:10]]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


tree.add_command(event_group)


# ==========================================================
#                    BACKGROUND WORKER
# ==========================================================


async def _delete_later(message: discord.Message, delay: int) -> None:
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except discord.HTTPException:
        pass


async def publish_scheduled_event(rec: EventRecord) -> None:
    current = _get_event(rec.message_id)
    if current is not None:
        rec = current
    if rec.status != "scheduled":
        return
    if rec.message_id in _PUBLISHING_EVENTS:
        return
    old_id = rec.message_id
    _PUBLISHING_EVENTS.add(old_id)
    try:
        guild = client.get_guild(rec.guild_id)
        if guild is None:
            raise RuntimeError("Сервер не найден в кэше бота.")

        channel = guild.get_channel(rec.channel_id)
        if not isinstance(channel, discord.TextChannel):
            fetched = await client.fetch_channel(rec.channel_id)
            if not isinstance(fetched, discord.TextChannel):
                raise RuntimeError("Канал публикации не найден или не является текстовым.")
            channel = fetched

        _log(f"[ClanEvent] publishing event id={old_id} channel={channel.id} guild={guild.id}")
        msg = await channel.send(
            content=f"<@&{EVENT_NOT_VOTED_ROLE_ID}>",
            embed=build_event_embed(guild, rec),
            view=EventView(),
            allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
        )

        _update_event_message_id(old_id, msg.id, status="active")
        _log(f"[ClanEvent] published scheduled event old_id={old_id} message_id={msg.id} channel={channel.id}")
    finally:
        _PUBLISHING_EVENTS.discard(old_id)


async def _publish_when_due(message_id: int) -> None:
    while not client.is_closed():
        rec = _get_event(message_id)
        if rec is None or rec.status != "scheduled":
            return
        wait = rec.start_ts - _now_ts()
        if wait <= 0:
            try:
                await publish_scheduled_event(rec)
            except Exception as e:
                _log(f"[ClanEvent] delayed publish failed event={message_id}: {type(e).__name__}: {e}")
            return
        await asyncio.sleep(min(max(wait, 1), 10))

async def _process_scheduled_publish(rec: EventRecord) -> None:
    if rec.status == "preparing":
        if rec.message_id not in _PREPARING_EVENTS:
            _schedule_prepare_job(rec.message_id)
        return
    if rec.status != "scheduled":
        return
    if _now_ts() < rec.start_ts:
        return

    try:
        await publish_scheduled_event(rec)
    except Exception as e:
        _log(f"[ClanEvent] publish failed event={rec.message_id}: {type(e).__name__}: {e}")


async def _process_event_reminders(rec: EventRecord) -> None:
    guild = client.get_guild(rec.guild_id)
    if guild is None:
        return
    channel = guild.get_channel(rec.channel_id)
    if not isinstance(channel, discord.TextChannel):
        try:
            fetched = await client.fetch_channel(rec.channel_id)
            channel = fetched if isinstance(fetched, discord.TextChannel) else None
        except Exception:
            return
    if channel is None:
        return

    if rec.status != "active":
        return

    now = _now_ts()
    if now >= rec.end_ts:
        return

    for offset in REMINDER_OFFSETS_SECONDS:
        remind_at = rec.end_ts - offset
        # Отправляем, если момент уже наступил, но отметки ещё не закрылись.
        if now >= remind_at and not _reminder_was_sent(rec.message_id, offset):
            msg = await channel.send(
                f"<@&{EVENT_NOT_VOTED_ROLE_ID}>, событие скоро начнется, мы ждем вашей отметки!",
                allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
            )
            _mark_reminder_sent(rec.message_id, offset, msg.id)
            asyncio.create_task(_delete_later(msg, REMINDER_DELETE_AFTER_SECONDS))


async def _process_event_cleanup(rec: EventRecord) -> None:
    if rec.status != "active":
        return
    if _now_ts() < rec.end_ts:
        return
    guild = client.get_guild(rec.guild_id)
    if guild is None:
        return
    try:
        await cleanup_event_roles(guild)
    except Exception as e:
        _log(f"[ClanEvent] cleanup failed event={rec.message_id}: {type(e).__name__}: {e}")
        return
    _mark_event_cleaned(rec.message_id)

    channel = guild.get_channel(rec.channel_id)
    if isinstance(channel, discord.TextChannel):
        try:
            msg = await channel.fetch_message(rec.message_id)
            await msg.edit(embed=build_event_embed(guild, rec), view=None)
        except Exception:
            pass


async def event_background_worker() -> None:
    await client.wait_until_ready()
    _log("[ClanEvent] background worker started")
    await _sync_time100(force=True)
    while not client.is_closed():
        try:
            await _sync_time100(force=False)
            for rec in _get_active_events():
                await _process_scheduled_publish(rec)
                await _process_event_reminders(rec)
                await _process_event_cleanup(rec)
        except Exception as e:
            _log(f"[ClanEvent] worker error: {type(e).__name__}: {e}")
        await asyncio.sleep(EVENT_WORKER_INTERVAL_SECONDS)


def setup_event_manager() -> None:
    event_db_init()
    client.add_view(EventView())
    _log("[ClanEvent] setup_event_manager loaded")
    for rec in _get_active_events():
        if rec.status == "preparing":
            _schedule_prepare_job(rec.message_id)
        elif rec.status == "scheduled":
            _schedule_publish_job(rec.message_id)
    if not getattr(client, "_clan_event_worker_started", False):
        setattr(client, "_clan_event_worker_started", True)
        client.loop.create_task(event_background_worker())
