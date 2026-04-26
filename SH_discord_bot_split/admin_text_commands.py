"""
Текстовые команды для ручного управления заявками.

Доступ к .block/.unblock разрешён только пользователю ADMIN_COMMANDS_ALLOWED_USER_ID.
.help показывает понятный список доступных команд.
"""

from __future__ import annotations

import re
import discord

ADMIN_COMMANDS_ALLOWED_USER_ID = 1105559182624694393
APPLICATION_BLOCK_ROLE_ID = 1498046779491356672

_BLOCK_RE = re.compile(r"^\s*\.(block|unblock)\s+(?:<@!?(\d{15,25})>|(\d{15,25}))\s*$", re.IGNORECASE)


def is_admin_text_command(content: str | None) -> bool:
    if not content:
        return False
    lowered = content.strip().lower()
    return lowered.startswith(".block") or lowered.startswith(".unblock") or lowered == ".help"


async def _delete_command_message(message: discord.Message) -> None:
    try:
        await message.delete()
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        pass


async def _send(channel: discord.abc.Messageable, text: str) -> None:
    try:
        await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        pass


def _parse_block_command(content: str | None) -> tuple[str | None, int | None]:
    match = _BLOCK_RE.match(content or "")
    if not match:
        return None, None

    action = (match.group(1) or "").lower()
    raw_id = match.group(2) or match.group(3)

    try:
        return action, int(raw_id)
    except (TypeError, ValueError):
        return action, None


async def _resolve_member(message: discord.Message, user_id: int) -> discord.Member | None:
    if not message.guild:
        return None

    for mentioned in getattr(message, "mentions", []) or []:
        if isinstance(mentioned, discord.Member) and mentioned.id == user_id:
            return mentioned

    member = message.guild.get_member(user_id)
    if member:
        return member

    try:
        return await message.guild.fetch_member(user_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


async def _send_help(message: discord.Message) -> None:
    text = (
        "**Список команд бота**\n\n"
        "**Обзвон:**\n"
        "`.vc <@tag>` — вызывает человека на обзвон.\n"
        "`.обзвон <@tag>` — вызывает человека на обзвон.\n"
        "`.obzvon <@tag>` — вызывает человека на обзвон.\n\n"
        "**Уведомление в ЛС:**\n"
        "`.call <@tag>` — отправляет пользователю напоминание ответить в заявке. Результат отправляется в канал `логи-причин`.\n\n"
        "**Блокировка доступа к заявкам:**\n"
        "`.block <@tag>` — выдаёт роль, блокирующую доступ к заявкам.\n"
        "`.unblock <@tag>` — забирает роль, блокирующую доступ к заявкам.\n\n"
        "**Статусы заявок:**\n"
        "`🆕・user` — новая заявка.\n"
        "`🔵・user` — пользователь ждёт ответа.\n"
        "`🟡・user` — модератор ответил."
    )
    await _send(message.channel, text)


async def handle_admin_text_command(client: discord.Client, message: discord.Message) -> bool:
    """
    Возвращает True, если сообщение было обработано как команда этого модуля.
    """
    if not is_admin_text_command(message.content):
        return False

    if (message.content or "").strip().lower() == ".help":
        await _send_help(message)
        return True

    # .block/.unblock работают только на сервере и только для конкретного пользователя.
    if not message.guild or not isinstance(message.author, discord.Member):
        return True

    if message.author.id != ADMIN_COMMANDS_ALLOWED_USER_ID:
        return True

    action, target_id = _parse_block_command(message.content)

    await _delete_command_message(message)

    if action is None or target_id is None:
        await _send(message.channel, "**Использование:** `.block @пользователь` или `.unblock @пользователь`")
        return True

    role = message.guild.get_role(APPLICATION_BLOCK_ROLE_ID)
    if role is None:
        await _send(message.channel, "**Неуспешно. Я не нашёл роль блокировки заявок на сервере.**")
        return True

    target = await _resolve_member(message, target_id)
    if target is None or getattr(target, "bot", False):
        await _send(message.channel, "**Неуспешно. Я не смог найти этого пользователя на сервере.**")
        return True

    try:
        if action == "block":
            if role not in target.roles:
                await target.add_roles(role, reason=f"[SH] Application access blocked by {message.author} ({message.author.id})")
            await _send(message.channel, f"**Успешно. Пользователю <@{target.id}> выдана роль блокировки доступа к заявкам.**")
        else:
            if role in target.roles:
                await target.remove_roles(role, reason=f"[SH] Application access unblocked by {message.author} ({message.author.id})")
            await _send(message.channel, f"**Успешно. У пользователя <@{target.id}> забрана роль блокировки доступа к заявкам.**")
    except discord.Forbidden:
        await _send(message.channel, "**Неуспешно. У меня нет прав выдать/забрать эту роль. Проверь права и позицию роли бота.**")
    except discord.HTTPException:
        await _send(message.channel, "**Неуспешно. Discord не дал выполнить действие, попробуйте ещё раз.**")

    return True
