from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

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
EVENT_WORKER_INTERVAL_SECONDS = 60
MASS_ROLE_DELAY_SECONDS = 0.25

DATE_FORMAT_HINT = "28.04.2026 18:00"


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
            "FROM clan_events WHERE status='active' ORDER BY start_ts ASC;"
        ).fetchall()
    return [EventRecord(*row) for row in rows]


def _has_active_event(guild_id: int) -> EventRecord | None:
    now = int(time.time())
    with _connect() as con:
        row = con.execute(
            "SELECT message_id, guild_id, channel_id, creator_id, title, description, start_ts, end_ts, member_limit, status, cleaned_at "
            "FROM clan_events WHERE guild_id=? AND status='active' AND end_ts>? ORDER BY start_ts ASC LIMIT 1;",
            (guild_id, now),
        ).fetchone()
    return EventRecord(*row) if row else None


def _set_event_status(message_id: int, status: str) -> None:
    with _connect() as con:
        con.execute("UPDATE clan_events SET status=? WHERE message_id=?;", (status, message_id))


def _mark_event_cleaned(message_id: int) -> None:
    with _connect() as con:
        con.execute(
            "UPDATE clan_events SET status='finished', cleaned_at=? WHERE message_id=?;",
            (int(time.time()), message_id),
        )


def _set_response(message_id: int, user_id: int, status: str) -> None:
    with _connect() as con:
        con.execute(
            "INSERT INTO clan_event_responses(message_id, user_id, status, updated_at) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(message_id, user_id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at;",
            (message_id, user_id, status, int(time.time())),
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
            (message_id, offset_seconds, sent_message_id, int(time.time())),
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

    now_ts = int(time.time())
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


async def assign_not_voted_to_humans(guild: discord.Guild) -> tuple[int, int]:
    roles = _get_event_roles(guild)
    if roles is None:
        raise RuntimeError("Одна или несколько event-ролей не найдены на сервере.")

    not_voted = roles["not_voted"]
    changed = 0
    failed = 0

    # На всякий случай просим Discord прогреть список участников.
    try:
        await guild.chunk(cache=True)
    except Exception:
        pass

    for member in guild.members:
        if member.bot or member.id in EVENT_ROLE_SKIP_USER_IDS:
            continue
        try:
            await _clear_event_roles(member, roles, reason="[Event] reset temporary event roles")
            if not_voted not in member.roles:
                await member.add_roles(not_voted, reason="[Event] event created: waiting for response")
                changed += 1
        except (discord.Forbidden, discord.HTTPException):
            failed += 1
        await asyncio.sleep(MASS_ROLE_DELAY_SECONDS)
    return changed, failed




async def verify_not_voted_roles(guild: discord.Guild) -> tuple[int, int]:
    """Проверяет, что всем подходящим людям выдана роль "ещё не отметился".

    Возвращает: (eligible_count, missing_count).
    """
    roles = _get_event_roles(guild)
    if roles is None:
        raise RuntimeError("Одна или несколько event-ролей не найдены на сервере.")

    try:
        await guild.chunk(cache=True)
    except Exception:
        pass

    not_voted = roles["not_voted"]
    eligible = 0
    missing = 0
    for member in guild.members:
        if member.bot or member.id in EVENT_ROLE_SKIP_USER_IDS:
            continue
        eligible += 1
        if not_voted not in member.roles:
            missing += 1
    return eligible, missing


async def cleanup_event_roles(guild: discord.Guild) -> tuple[int, int]:
    roles = _get_event_roles(guild)
    if roles is None:
        raise RuntimeError("Одна или несколько event-ролей не найдены на сервере.")

    changed = 0
    failed = 0
    try:
        await guild.chunk(cache=True)
    except Exception:
        pass

    for member in guild.members:
        if member.bot or member.id in EVENT_ROLE_SKIP_USER_IDS:
            continue
        try:
            before = len([r for r in roles.values() if r in member.roles])
            await _clear_event_roles(member, roles, reason="[Event] event finished/deleted: cleanup temporary roles")
            if before:
                changed += 1
        except (discord.Forbidden, discord.HTTPException):
            failed += 1
        await asyncio.sleep(MASS_ROLE_DELAY_SECONDS)
    return changed, failed


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
            if start_dt <= now:
                raise ValueError("Событие нельзя создать в прошлом.")
            if end_dt <= start_dt:
                raise ValueError("Поле 'Когда закрыть отметки?' должно быть позже поля 'Когда опубликовать событие?'. Например: публикация 28.04.2026 18:00, закрытие 28.04.2026 20:00.")
            if not title or not desc:
                raise ValueError("Название и описание не могут быть пустыми.")
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            changed, failed = await assign_not_voted_to_humans(interaction.guild)
            eligible, missing = await verify_not_voted_roles(interaction.guild)
        except Exception as e:
            await interaction.followup.send(
                f"❌ Не смог выдать роль 'ещё не отметился'. Проверь права бота и расположение роли бота выше временных ролей.\nОшибка: `{type(e).__name__}: {e}`",
                ephemeral=True,
            )
            return

        if failed or missing:
            try:
                await cleanup_event_roles(interaction.guild)
            except Exception:
                pass
            await interaction.followup.send(
                "❌ Событие не опубликовано, потому что бот не смог корректно выдать роль 'ещё не отметился' всем нужным людям.\n"
                f"Проверено людей: {eligible}. Не получили роль: {missing}. Ошибок выдачи: {failed}.\n"
                "Проверь права бота, SERVER MEMBERS INTENT и чтобы роль бота была выше event-ролей.",
                ephemeral=True,
            )
            return

        temp_rec = EventRecord(
            message_id=0,
            guild_id=interaction.guild.id,
            channel_id=interaction.channel.id,
            creator_id=interaction.user.id,
            title=title,
            description=desc,
            start_ts=int(start_dt.timestamp()),
            end_ts=int(end_dt.timestamp()),
            member_limit=limit,
            status="active",
            cleaned_at=None,
        )

        # Сначала отправляем placeholder, затем сохраняем message_id и редактируем embed с корректным id.
        msg = await interaction.channel.send(
            content=f"<@&{EVENT_NOT_VOTED_ROLE_ID}>",
            allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
        )
        rec = EventRecord(
            message_id=msg.id,
            guild_id=temp_rec.guild_id,
            channel_id=temp_rec.channel_id,
            creator_id=temp_rec.creator_id,
            title=temp_rec.title,
            description=temp_rec.description,
            start_ts=temp_rec.start_ts,
            end_ts=temp_rec.end_ts,
            member_limit=temp_rec.member_limit,
            status=temp_rec.status,
            cleaned_at=temp_rec.cleaned_at,
        )
        _insert_event(rec)
        await msg.edit(embed=build_event_embed(interaction.guild, rec), view=EventView())

        result = f"✅ Событие создано. Роль 'ещё не отметился' выдана и проверена у людей: {eligible}."
        await interaction.followup.send(result, ephemeral=True)


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
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        changed, failed = await cleanup_event_roles(interaction.guild)
    except Exception as e:
        await interaction.followup.send(f"❌ Не смог очистить роли: `{type(e).__name__}: {e}`", ephemeral=True)
        return
    await interaction.followup.send(f"✅ Временные роли сняты у {changed} пользователей. Ошибок: {failed}.", ephemeral=True)


@event_group.command(name="list", description="Показать активные события")
async def event_list(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("❌ Команда работает только на сервере.", ephemeral=True)
        return
    events = [e for e in _get_active_events() if e.guild_id == interaction.guild.id]
    if not events:
        await interaction.response.send_message("Активных событий нет.", ephemeral=True)
        return
    lines = [f"• **{e.title}** — <t:{e.start_ts}:R> | канал <#{e.channel_id}>" for e in events[:10]]
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

    now = int(time.time())
    if now >= rec.start_ts:
        return

    for offset in REMINDER_OFFSETS_SECONDS:
        remind_at = rec.start_ts - offset
        # Отправляем, если момент уже наступил, но событие ещё не началось.
        if now >= remind_at and not _reminder_was_sent(rec.message_id, offset):
            msg = await channel.send(
                f"<@&{EVENT_NOT_VOTED_ROLE_ID}>, событие скоро начнется, мы ждем вашей отметки!",
                allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
            )
            _mark_reminder_sent(rec.message_id, offset, msg.id)
            asyncio.create_task(_delete_later(msg, REMINDER_DELETE_AFTER_SECONDS))


async def _process_event_cleanup(rec: EventRecord) -> None:
    if int(time.time()) < rec.end_ts:
        return
    guild = client.get_guild(rec.guild_id)
    if guild is None:
        return
    try:
        await cleanup_event_roles(guild)
    except Exception as e:
        print(f"[ClanEvent] cleanup failed event={rec.message_id}: {type(e).__name__}: {e}")
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
    while not client.is_closed():
        try:
            for rec in _get_active_events():
                await _process_event_reminders(rec)
                await _process_event_cleanup(rec)
        except Exception as e:
            print(f"[ClanEvent] worker error: {type(e).__name__}: {e}")
        await asyncio.sleep(EVENT_WORKER_INTERVAL_SECONDS)


def setup_event_manager() -> None:
    event_db_init()
    client.add_view(EventView())
    if not getattr(client, "_clan_event_worker_started", False):
        setattr(client, "_clan_event_worker_started", True)
        client.loop.create_task(event_background_worker())
