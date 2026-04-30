from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import dataclass

import discord

from config import (
    DB_PATH,
    PROTECTED_GUILD_LOG_CHANNELS,
    PROTECT_PING_USER_ID,
)

# Анти-дребезг: Discord может прислать несколько update-событий за одно перетаскивание.
_RESTORE_DELAY_SECONDS = 1.2
_AUDIT_LOOKBACK_SECONDS = 20

_restoring_guilds: set[int] = set()
_restore_tasks: dict[int, asyncio.Task] = {}
_last_snapshot_at: dict[int, float] = {}


@dataclass(slots=True)
class ChannelSnapshot:
    channel_id: int
    name: str
    type_name: str
    category_id: int | None
    position: int


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def protection_db_init() -> None:
    with _connect() as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS channel_protection ("
            "guild_id INTEGER PRIMARY KEY, "
            "enabled INTEGER NOT NULL DEFAULT 0, "
            "snapshot_json TEXT NOT NULL DEFAULT '[]', "
            "updated_at INTEGER NOT NULL DEFAULT 0"
            ");"
        )


def _is_supported_guild(guild_id: int) -> bool:
    return int(guild_id) in PROTECTED_GUILD_LOG_CHANNELS


def is_protection_enabled(guild_id: int) -> bool:
    if not _is_supported_guild(guild_id):
        return False
    with _connect() as con:
        row = con.execute(
            "SELECT enabled FROM channel_protection WHERE guild_id=?;",
            (int(guild_id),),
        ).fetchone()
    return bool(row and int(row[0]) == 1)


def _channel_type_name(channel: discord.abc.GuildChannel) -> str:
    if isinstance(channel, discord.CategoryChannel):
        return "category"
    if isinstance(channel, discord.TextChannel):
        return "text"
    if isinstance(channel, discord.VoiceChannel):
        return "voice"
    if isinstance(channel, discord.StageChannel):
        return "stage"
    if isinstance(channel, discord.ForumChannel):
        return "forum"
    return channel.__class__.__name__.lower()


def _build_snapshot(guild: discord.Guild) -> list[ChannelSnapshot]:
    items: list[ChannelSnapshot] = []
    for ch in guild.channels:
        # Threads не входят в guild.channels, поэтому тут обычные каналы/категории.
        items.append(
            ChannelSnapshot(
                channel_id=ch.id,
                name=getattr(ch, "name", "") or "",
                type_name=_channel_type_name(ch),
                category_id=getattr(ch, "category_id", None),
                position=int(getattr(ch, "position", 0) or 0),
            )
        )
    items.sort(key=lambda x: (x.category_id or 0, x.position, x.channel_id))
    return items


def _snapshot_to_json(snapshot: list[ChannelSnapshot]) -> str:
    return json.dumps(
        [
            {
                "channel_id": item.channel_id,
                "name": item.name,
                "type_name": item.type_name,
                "category_id": item.category_id,
                "position": item.position,
            }
            for item in snapshot
        ],
        ensure_ascii=False,
    )


def _snapshot_from_json(raw: str | None) -> list[ChannelSnapshot]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    out: list[ChannelSnapshot] = []
    for item in data if isinstance(data, list) else []:
        try:
            out.append(
                ChannelSnapshot(
                    channel_id=int(item["channel_id"]),
                    name=str(item.get("name") or ""),
                    type_name=str(item.get("type_name") or ""),
                    category_id=int(item["category_id"]) if item.get("category_id") is not None else None,
                    position=int(item.get("position") or 0),
                )
            )
        except Exception:
            continue
    return out


def get_saved_snapshot(guild_id: int) -> list[ChannelSnapshot]:
    with _connect() as con:
        row = con.execute(
            "SELECT snapshot_json FROM channel_protection WHERE guild_id=?;",
            (int(guild_id),),
        ).fetchone()
    return _snapshot_from_json(row[0]) if row else []


def set_protection_enabled(guild: discord.Guild, enabled: bool) -> int:
    """Включает/выключает защиту. При включении запоминает текущее расположение каналов."""
    protection_db_init()
    if not _is_supported_guild(guild.id):
        raise ValueError("Этот сервер не добавлен в список защищаемых серверов.")

    now = int(time.time())
    snapshot_json = _snapshot_to_json(_build_snapshot(guild)) if enabled else "[]"
    with _connect() as con:
        con.execute(
            "INSERT INTO channel_protection(guild_id, enabled, snapshot_json, updated_at) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET enabled=excluded.enabled, snapshot_json=excluded.snapshot_json, updated_at=excluded.updated_at;",
            (int(guild.id), 1 if enabled else 0, snapshot_json, now),
        )
    if enabled:
        _last_snapshot_at[guild.id] = time.time()
        return len(_build_snapshot(guild))
    return 0


async def ensure_enabled_snapshots(client: discord.Client) -> None:
    """После рестарта убеждаемся, что таблица есть. Снимок НЕ перезаписываем, чтобы защита помнила старый порядок."""
    protection_db_init()
    for guild_id in PROTECTED_GUILD_LOG_CHANNELS:
        guild = client.get_guild(int(guild_id))
        if not guild:
            continue
        if is_protection_enabled(guild.id) and not get_saved_snapshot(guild.id):
            # Если защита была включена, но снимка нет — безопасно создаём текущий.
            set_protection_enabled(guild, True)


async def _find_actor(guild: discord.Guild, *, action: discord.AuditLogAction, target_id: int) -> discord.User | discord.Member | None:
    try:
        async for entry in guild.audit_logs(limit=8, action=action):
            created_ts = entry.created_at.timestamp() if entry.created_at else 0
            if abs(time.time() - created_ts) > _AUDIT_LOOKBACK_SECONDS:
                continue
            target = getattr(entry, "target", None)
            if getattr(target, "id", None) == target_id:
                return entry.user
    except (discord.Forbidden, discord.HTTPException):
        return None
    except Exception:
        return None
    return None


def _user_text(user: discord.User | discord.Member | None) -> str:
    if not user:
        return "`не удалось определить`"
    return f"{user.mention} (`{user}` / `{user.id}`)"


def _channel_ref(channel_id: int, name: str | None = None) -> str:
    if name:
        return f"<#{channel_id}> (`{name}` / `{channel_id}`)"
    return f"<#{channel_id}> (`{channel_id}`)"


async def _send_log(guild: discord.Guild, text: str) -> None:
    log_channel_id = PROTECTED_GUILD_LOG_CHANNELS.get(int(guild.id))
    if not log_channel_id:
        return
    channel = guild.get_channel(int(log_channel_id))
    if channel is None:
        try:
            channel = await guild.fetch_channel(int(log_channel_id))
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            return
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return

    ping = f"<@{PROTECT_PING_USER_ID}>" if PROTECT_PING_USER_ID else ""
    try:
        await channel.send(
            f"{ping}\n{text}".strip(),
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )
    except discord.HTTPException:
        pass


async def log_protection_command(guild: discord.Guild, *, enabled: bool, moderator: discord.abc.User, channel_count: int = 0) -> None:
    status = "🛡️ **Защита каналов включена**" if enabled else "🔓 **Защита каналов выключена**"
    extra = f"\n> Зафиксировано каналов/категорий: `{channel_count}`" if enabled else "\n> Теперь каналы можно двигать и переименовывать без отката порядка."
    await _send_log(
        guild,
        f"{status}\n"
        f"> Сервер: **{guild.name}** (`{guild.id}`)\n"
        f"> Команду выполнил: {_user_text(moderator)}"
        f"{extra}",
    )


async def _restore_guild_layout(guild: discord.Guild, *, reason: str) -> None:
    if guild.id in _restoring_guilds:
        return
    if not is_protection_enabled(guild.id):
        return

    snapshot = get_saved_snapshot(guild.id)
    if not snapshot:
        return

    _restoring_guilds.add(guild.id)
    try:
        by_id = {ch.id: ch for ch in guild.channels}

        # 1) Возвращаем канал в исходную категорию, если его перекинули.
        # Название специально НЕ откатываем: по ТЗ названия логируем, а не запрещаем.
        for item in snapshot:
            ch = by_id.get(item.channel_id)
            if ch is None or isinstance(ch, discord.CategoryChannel):
                continue
            current_category_id = getattr(ch, "category_id", None)
            if current_category_id != item.category_id:
                category = guild.get_channel(item.category_id) if item.category_id else None
                if item.category_id is None or isinstance(category, discord.CategoryChannel):
                    try:
                        await ch.edit(category=category, reason=reason)
                        await asyncio.sleep(0.35)
                    except (discord.Forbidden, discord.HTTPException):
                        pass

        # 2) Возвращаем позиции. Категории сначала, потом обычные каналы.
        refreshed = {ch.id: ch for ch in guild.channels}
        for item in sorted(snapshot, key=lambda x: (0 if x.type_name == "category" else 1, x.category_id or 0, x.position, x.channel_id)):
            ch = refreshed.get(item.channel_id)
            if ch is None:
                continue
            try:
                if int(getattr(ch, "position", 0) or 0) != item.position:
                    await ch.edit(position=item.position, reason=reason)
                    await asyncio.sleep(0.35)
            except (discord.Forbidden, discord.HTTPException):
                pass
    finally:
        await asyncio.sleep(0.5)
        _restoring_guilds.discard(guild.id)


def _schedule_restore(guild: discord.Guild, *, reason: str) -> None:
    old = _restore_tasks.get(guild.id)
    if old and not old.done():
        old.cancel()

    async def runner() -> None:
        try:
            await asyncio.sleep(_RESTORE_DELAY_SECONDS)
            await _restore_guild_layout(guild, reason=reason)
        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"[ChannelProtection] restore failed guild={guild.id}: {type(e).__name__}: {e}", flush=True)

    try:
        _restore_tasks[guild.id] = asyncio.create_task(runner())
    except RuntimeError:
        _restore_tasks[guild.id] = guild._state.loop.create_task(runner())  # type: ignore[attr-defined]


def _layout_changed(before: discord.abc.GuildChannel, after: discord.abc.GuildChannel) -> bool:
    return (
        int(getattr(before, "position", 0) or 0) != int(getattr(after, "position", 0) or 0)
        or getattr(before, "category_id", None) != getattr(after, "category_id", None)
    )


def _name_changed(before: discord.abc.GuildChannel, after: discord.abc.GuildChannel) -> bool:
    return (getattr(before, "name", "") or "") != (getattr(after, "name", "") or "")


async def handle_guild_channel_update(before: discord.abc.GuildChannel, after: discord.abc.GuildChannel) -> None:
    guild = after.guild
    if guild.id in _restoring_guilds:
        return
    if not is_protection_enabled(guild.id):
        return

    actor = None
    if _layout_changed(before, after) or _name_changed(before, after):
        actor = await _find_actor(guild, action=discord.AuditLogAction.channel_update, target_id=after.id)

    if _layout_changed(before, after):
        await _send_log(
            guild,
            "🚨 **Попытка изменить порядок/категорию канала**\n"
            f"> Канал: {_channel_ref(after.id, getattr(after, 'name', None))}\n"
            f"> Было: категория `{getattr(before, 'category_id', None)}`, позиция `{getattr(before, 'position', None)}`\n"
            f"> Стало: категория `{getattr(after, 'category_id', None)}`, позиция `{getattr(after, 'position', None)}`\n"
            f"> Изменил: {_user_text(actor)}\n"
            "> Действие: **возвращаю канал на сохранённое место**",
        )
        _schedule_restore(guild, reason=f"[SH Protect] Channel order/category restored after protected update")

    if _name_changed(before, after):
        await _send_log(
            guild,
            "✏️ **Изменено название канала**\n"
            f"> Канал: {_channel_ref(after.id, getattr(after, 'name', None))}\n"
            f"> Было: `#{getattr(before, 'name', '')}`\n"
            f"> Стало: `#{getattr(after, 'name', '')}`\n"
            f"> Изменил: {_user_text(actor)}",
        )


async def handle_guild_channel_create(channel: discord.abc.GuildChannel) -> None:
    guild = channel.guild
    if guild.id in _restoring_guilds:
        return
    if not is_protection_enabled(guild.id):
        return
    actor = await _find_actor(guild, action=discord.AuditLogAction.channel_create, target_id=channel.id)
    await _send_log(
        guild,
        "➕ **Создан новый канал при включённой защите**\n"
        f"> Канал: {_channel_ref(channel.id, getattr(channel, 'name', None))}\n"
        f"> Создал: {_user_text(actor)}\n"
        "> Важно: новый канал не входит в сохранённый порядок. Чтобы зафиксировать новый порядок, используй `.protect_on <id сервера>` ещё раз.",
    )


async def handle_guild_channel_delete(channel: discord.abc.GuildChannel) -> None:
    guild = channel.guild
    if guild.id in _restoring_guilds:
        return
    if not is_protection_enabled(guild.id):
        return
    actor = await _find_actor(guild, action=discord.AuditLogAction.channel_delete, target_id=channel.id)
    await _send_log(
        guild,
        "🗑️ **Удалён канал при включённой защите**\n"
        f"> Канал: `#{getattr(channel, 'name', '')}` (`{channel.id}`)\n"
        f"> Удалил: {_user_text(actor)}\n"
        "> Бот не может восстановить удалённый канал полностью автоматически, но действие залогировано.",
    )
