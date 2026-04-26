# ticket_status.py
"""
Статусы каналов заявок.

Использует именно Discord username (`user.name`), а не display name / nick.
Пример названий:
- 🆕-username  — заявка создана
- 🟡-username  — пользователь ждёт ответа
- 💛-username  — модератор ответил
"""

from __future__ import annotations

import re
import discord

STATUS_CREATED = "created"
STATUS_USER_WAITING = "user_waiting"
STATUS_MOD_ANSWERED = "mod_answered"

_STATUS_EMOJI = {
    STATUS_CREATED: "🆕",
    STATUS_USER_WAITING: "🟡",
    STATUS_MOD_ANSWERED: "💛",
}

_STATUS_PREFIXES = tuple(f"{emoji}-" for emoji in _STATUS_EMOJI.values())


def is_status_ticket_name(channel_name: str) -> bool:
    """True, если канал уже назван через нашу систему статусов."""
    return (channel_name or "").startswith(_STATUS_PREFIXES)


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
    return f"{emoji}-{_clean_username(opener)}"[:100]


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


async def move_application_channel_to_top(channel: discord.TextChannel) -> bool:
    """
    Поднимает канал заявки в самый верх текущей категории.
    Используется, когда пользователь написал и ждёт ответа модератора.
    """
    category = getattr(channel, "category", None)
    if category is None:
        return False

    try:
        text_channels = sorted(category.text_channels, key=lambda c: c.position)
        if text_channels and text_channels[0].id == channel.id:
            return False
    except Exception:
        pass

    try:
        await channel.move(
            beginning=True,
            category=category,
            reason="[SH] User is waiting for moderator response",
        )
        return True
    except TypeError:
        try:
            await channel.move(
                beginning=True,
                reason="[SH] User is waiting for moderator response",
            )
            return True
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"[SH] WARNING: failed to move application channel {channel.id} to top: {type(e).__name__}: {e}")
    except discord.Forbidden:
        print(f"[SH] WARNING: bot has no permission to move application channel {channel.id}")
    except discord.HTTPException as e:
        print(f"[SH] WARNING: failed to move application channel {channel.id} to top: {type(e).__name__}: {e}")
    except Exception as e:
        print(f"[SH] WARNING: unexpected move error channel={channel.id}: {type(e).__name__}: {e}")

    return False
