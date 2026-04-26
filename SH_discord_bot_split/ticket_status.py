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

import re
import discord

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


async def set_ticket_channel_status(
    channel: discord.TextChannel,
    opener: discord.abc.User | None,
    status: str,
) -> bool:
    """
    Переименовывает канал заявки под нужный статус.
    Возвращает True, если реально переименовал канал.
    """
    if opener is None:
        return False

    new_name = build_ticket_channel_name(status, opener)

    if channel.name == new_name:
        return False

    try:
        await channel.edit(name=new_name, reason="[SH] Application status update")
        return True
    except discord.Forbidden:
        print(f"[SH] WARNING: bot has no permission to rename application channel {channel.id}")
    except discord.HTTPException as e:
        print(f"[SH] WARNING: failed to rename application channel {channel.id}: {type(e).__name__}: {e}")
    except Exception as e:
        print(f"[SH] WARNING: unexpected rename error channel={channel.id}: {type(e).__name__}: {e}")

    return False


def _is_reason_logs_channel(ch: discord.TextChannel) -> bool:
    """Ищем канал логов причин по названию, даже если есть эмодзи/точки/пробелы."""
    name = (getattr(ch, "name", "") or "").lower().replace(" ", "")
    return "логи-причин" in name or "логипричин" in name


async def move_application_channel_to_top(channel: discord.TextChannel) -> bool:
    """
    Поднимает канал заявки наверх категории, но оставляет его ниже канала "логи-причин".
    Используется, когда пользователь написал и ждёт ответа модератора.
    """
    category = getattr(channel, "category", None)
    if category is None:
        return False

    def sorted_text_channels() -> list[discord.TextChannel]:
        return sorted(list(category.text_channels), key=lambda c: c.position)

    def find_log_channel() -> discord.TextChannel | None:
        for ch in sorted_text_channels():
            if ch.id != channel.id and _is_reason_logs_channel(ch):
                return ch
        return None

    def already_ok(log_channel: discord.TextChannel | None) -> bool:
        channels = sorted_text_channels()
        if log_channel is not None:
            for index, ch in enumerate(channels):
                if ch.id == log_channel.id:
                    return index + 1 < len(channels) and channels[index + 1].id == channel.id
            return False
        return bool(channels and channels[0].id == channel.id)

    log_channel = find_log_channel()
    if already_ok(log_channel):
        return False

    reason = "[SH] User is waiting for moderator response"

    try:
        if log_channel is not None:
            await channel.move(after=log_channel, reason=reason)
        else:
            await channel.move(beginning=True, reason=reason)
        return True
    except TypeError:
        pass
    except discord.Forbidden:
        print(f"[SH] WARNING: bot has no permission to move application channel {channel.id}")
        return False
    except discord.HTTPException as e:
        print(f"[SH] WARNING: failed to move application channel {channel.id} near top: {type(e).__name__}: {e}")
        return False
    except Exception as e:
        print(f"[SH] WARNING: unexpected move-near-top error channel={channel.id}: {type(e).__name__}: {e}")

    try:
        log_channel = find_log_channel()
        target_position = (log_channel.position + 1) if log_channel is not None else 0
        await channel.edit(position=target_position, reason=reason)
        return True
    except discord.Forbidden:
        print(f"[SH] WARNING: bot has no permission to move application channel {channel.id}")
    except discord.HTTPException as e:
        print(f"[SH] WARNING: failed to edit application channel position {channel.id}: {type(e).__name__}: {e}")
    except Exception as e:
        print(f"[SH] WARNING: unexpected edit-position error channel={channel.id}: {type(e).__name__}: {e}")

    return False
