# ticket_status.py
"""
Статусы каналов заявок.

Использует именно Discord username (`user.name`), а не display name / nick.
Пример названий:
- 🆕・username  — заявка создана
- 🔵・username  — пользователь ждёт ответа
- 🟡・username  — модератор ответил
"""

from __future__ import annotations

import asyncio
import time
import re
import discord

from db import db_delete_prompt, db_delete_ticket

STATUS_CREATED = "created"
STATUS_USER_WAITING = "user_waiting"
STATUS_MOD_ANSWERED = "mod_answered"

_STATUS_EMOJI = {
    STATUS_CREATED: "🆕",
    STATUS_USER_WAITING: "🔵",
    STATUS_MOD_ANSWERED: "🟡",
}

_STATUS_PREFIXES = tuple(f"{emoji}・" for emoji in _STATUS_EMOJI.values())
_OLD_STATUS_PREFIXES = tuple(f"{emoji}-" for emoji in _STATUS_EMOJI.values())

# Защита от Discord rate limit на PATCH /channels/{id}:
# - один lock на канал, чтобы несколько сообщений не запускали channel.edit одновременно;
# - общий короткий cooldown после PATCH;
# - отдельный cooldown на движение канала вверх, потому что его чаще всего долбят на каждое сообщение.
_CHANNEL_LOCKS: dict[int, asyncio.Lock] = {}
_LAST_CHANNEL_PATCH_AT: dict[int, float] = {}
_LAST_MOVE_ATTEMPT_AT: dict[int, float] = {}
PATCH_COOLDOWN_SECONDS = 20.0
# Discord очень жёстко лимитит PATCH /channels при движении каналов в категории.
# Поэтому автоподнятие заявки вверх отключено по умолчанию: статус в названии остаётся,
# а лишние PATCH position больше не душат бота на 3-10 минут.
MOVE_CHANNELS_UNDER_LOGS = False
MOVE_COOLDOWN_SECONDS = 3600.0


def _get_channel_lock(channel_id: int) -> asyncio.Lock:
    lock = _CHANNEL_LOCKS.get(channel_id)
    if lock is None:
        lock = asyncio.Lock()
        _CHANNEL_LOCKS[channel_id] = lock
    return lock


def is_status_ticket_name(channel_name: str) -> bool:
    """True, если канал уже назван через нашу систему статусов."""
    return (channel_name or "").startswith(_STATUS_PREFIXES + _OLD_STATUS_PREFIXES)


def _clean_username(user: discord.abc.User) -> str:
    """
    Берём именно username Discord: user.name.
    Не используем display_name, nick, global_name и старый tag с #.
    """
    username = (getattr(user, "name", None) or str(user) or str(getattr(user, "id", "user"))).lower()

    username = username.split("#", 1)[0]
    username = re.sub(r"[^a-z0-9_-]+", "-", username)
    username = re.sub(r"-+", "-", username).strip("-")

    if not username:
        username = str(getattr(user, "id", "user"))

    return username[:80]


def build_ticket_channel_name(status: str, opener: discord.abc.User) -> str:
    emoji = _STATUS_EMOJI.get(status, _STATUS_EMOJI[STATUS_CREATED])
    return f"{emoji}・{_clean_username(opener)}"[:100]


def _is_reason_logs_channel(ch: discord.TextChannel) -> bool:
    """Ищем канал логов причин по названию, даже если есть эмодзи/точки/пробелы."""
    name = (getattr(ch, "name", "") or "").lower().replace(" ", "")
    return "логи-причин" in name or "логипричин" in name


def _category_channels_sorted(channel: discord.TextChannel) -> list[discord.TextChannel]:
    category = getattr(channel, "category", None)
    if category is None:
        return []
    return sorted(list(category.text_channels), key=lambda c: c.position)


def _find_reason_logs_channel(channel: discord.TextChannel) -> discord.TextChannel | None:
    for ch in _category_channels_sorted(channel):
        if ch.id != channel.id and _is_reason_logs_channel(ch):
            return ch
    return None


def _is_channel_directly_under_logs(channel: discord.TextChannel) -> bool:
    channels = _category_channels_sorted(channel)
    if not channels:
        return False

    log_channel = _find_reason_logs_channel(channel)
    if log_channel is not None:
        for index, ch in enumerate(channels):
            if ch.id == log_channel.id:
                return index + 1 < len(channels) and channels[index + 1].id == channel.id
        return False

    return channels[0].id == channel.id


def _target_position_under_logs(channel: discord.TextChannel) -> int:
    log_channel = _find_reason_logs_channel(channel)
    if log_channel is not None:
        return log_channel.position + 1
    return 0


async def update_ticket_channel_status(
    channel: discord.TextChannel,
    opener: discord.abc.User | None,
    status: str,
    *,
    move_under_reason_logs: bool = False,
) -> bool:
    """
    Обновляет статус заявки.

    Если нужно и переименовать, и поднять канал — бот делает это одним channel.edit(...),
    а не двумя PATCH подряд. Это защищает от rate limit PATCH /channels/{id} -> 429.
    """
    if opener is None:
        return False

    lock = _get_channel_lock(channel.id)

    async with lock:
        new_name = build_ticket_channel_name(status, opener)
        need_rename = channel.name != new_name
        need_move = bool(move_under_reason_logs and not _is_channel_directly_under_logs(channel))

        if not need_rename and not need_move:
            return False

        now = time.monotonic()

        if need_move and not MOVE_CHANNELS_UNDER_LOGS:
            need_move = False
            if not need_rename:
                return False

        if need_move:
            last_move = _LAST_MOVE_ATTEMPT_AT.get(channel.id, 0.0)
            if now - last_move < MOVE_COOLDOWN_SECONDS:
                # Переименование можно сделать, но повторное движение пропускаем.
                need_move = False
                if not need_rename:
                    return False
            else:
                _LAST_MOVE_ATTEMPT_AT[channel.id] = now

        last_patch = _LAST_CHANNEL_PATCH_AT.get(channel.id, 0.0)
        if now - last_patch < PATCH_COOLDOWN_SECONDS:
            return False

        kwargs = {"reason": "[SH] Application status update"}
        if need_rename:
            kwargs["name"] = new_name
        if need_move:
            kwargs["position"] = _target_position_under_logs(channel)
            kwargs["reason"] = "[SH] User is waiting for moderator response"

        try:
            await channel.edit(**kwargs)
            _LAST_CHANNEL_PATCH_AT[channel.id] = time.monotonic()
            return True
        except discord.NotFound:
            # Канал уже удалён, чистим старые записи, чтобы бот не пытался работать с ним дальше.
            db_delete_ticket(channel.id)
            db_delete_prompt(channel.id)
            print(f"[SH] WARNING: application channel {channel.id} was deleted; removed stale DB records")
        except discord.Forbidden:
            print(f"[SH] WARNING: bot has no permission to edit application channel {channel.id}")
        except discord.HTTPException as e:
            print(f"[SH] WARNING: failed to edit application channel {channel.id}: {type(e).__name__}: {e}")
        except Exception as e:
            print(f"[SH] WARNING: unexpected channel-edit error channel={channel.id}: {type(e).__name__}: {e}")

        return False


async def set_ticket_channel_status(
    channel: discord.TextChannel,
    opener: discord.abc.User | None,
    status: str,
) -> bool:
    """
    Переименовывает канал заявки под нужный статус.
    Возвращает True, если реально переименовал канал.
    """
    return await update_ticket_channel_status(channel, opener, status, move_under_reason_logs=False)


async def move_application_channel_to_top(channel: discord.TextChannel) -> bool:
    """
    Поднимает канал заявки наверх категории, но оставляет его ниже канала "логи-причин".
    Оставлено для совместимости, но теперь с lock/cooldown.
    """
    lock = _get_channel_lock(channel.id)

    async with lock:
        if not MOVE_CHANNELS_UNDER_LOGS:
            return False

        if _is_channel_directly_under_logs(channel):
            return False

        now = time.monotonic()
        last_move = _LAST_MOVE_ATTEMPT_AT.get(channel.id, 0.0)
        if now - last_move < MOVE_COOLDOWN_SECONDS:
            return False

        last_patch = _LAST_CHANNEL_PATCH_AT.get(channel.id, 0.0)
        if now - last_patch < PATCH_COOLDOWN_SECONDS:
            return False

        _LAST_MOVE_ATTEMPT_AT[channel.id] = now

        try:
            await channel.edit(
                position=_target_position_under_logs(channel),
                reason="[SH] User is waiting for moderator response",
            )
            _LAST_CHANNEL_PATCH_AT[channel.id] = time.monotonic()
            return True
        except discord.NotFound:
            db_delete_ticket(channel.id)
            db_delete_prompt(channel.id)
            print(f"[SH] WARNING: application channel {channel.id} was deleted; removed stale DB records")
        except discord.Forbidden:
            print(f"[SH] WARNING: bot has no permission to move application channel {channel.id}")
        except discord.HTTPException as e:
            print(f"[SH] WARNING: failed to move application channel {channel.id}: {type(e).__name__}: {e}")
        except Exception as e:
            print(f"[SH] WARNING: unexpected move error channel={channel.id}: {type(e).__name__}: {e}")

        return False
