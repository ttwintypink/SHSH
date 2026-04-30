"""
Текстовые команды для ручного управления заявками.

Доступ к .block/.unblock/.privatkaform разрешён только пользователю ADMIN_COMMANDS_ALLOWED_USER_ID.
.help показывает понятный список доступных команд.
"""

from __future__ import annotations

import re
import discord

from command_reports import build_report, send_report
from member_cache import safe_fetch_member
from privatka import ensure_private_setup_message
from channel_protection import set_protection_enabled, log_protection_command
from config import PROTECTED_GUILD_LOG_CHANNELS

ADMIN_COMMANDS_ALLOWED_USER_ID = 1105559182624694393
APPLICATION_BLOCK_ROLE_ID = 1498046779491356672

_BLOCK_RE = re.compile(r"^\s*\.(block|unblock)\s+(?:<@!?(\d{15,25})>|(\d{15,25}))\s*$", re.IGNORECASE)
_PROTECT_RE = re.compile(r"^\s*\.(protect_on|protect_off)\s+(\d{15,25})\s*$", re.IGNORECASE)


def is_admin_text_command(content: str | None) -> bool:
    if not content:
        return False
    lowered = content.strip().lower()
    return (
        lowered.startswith(".block")
        or lowered.startswith(".unblock")
        or lowered.startswith(".protect_on")
        or lowered.startswith(".protect_off")
        or lowered == ".help"
        or lowered == ".privatkaform"
    )


async def _delete_command_message(message: discord.Message) -> None:
    try:
        await message.delete()
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        pass


async def _send(channel: discord.abc.Messageable, text: str) -> None:
    try:
        await channel.send(text, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
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


def _parse_protect_command(content: str | None) -> tuple[str | None, int | None]:
    match = _PROTECT_RE.match(content or "")
    if not match:
        return None, None
    action = (match.group(1) or "").lower()
    raw_id = match.group(2)
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

    return await safe_fetch_member(message.guild, user_id, allow_fetch=True)


def _block_report(action: str, ok: bool, moderator_id: int, target_id: int | None, details: str) -> str:
    if action == "block":
        title = "✅ **・Доступ к заявкам заблокирован**" if ok else "❌ **・Доступ к заявкам не заблокирован**"
        command = ".block"
    else:
        title = "✅ **・Доступ к заявкам разблокирован**" if ok else "❌ **・Доступ к заявкам не разблокирован**"
        command = ".unblock"

    return build_report(title, moderator_id, target_id, command, ok, details)


async def _send_help(message: discord.Message) -> None:
    text = (
        "🌨️ **・Панель команд SH**\n"
        "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
        "┃ 📞 **Обзвон**\n"
        "┃ `.vc <@tag>` — вызывает человека на обзвон\n"
        "┃ `.obzvon <@tag>` — вызывает человека на обзвон\n"
        "┃ `.обзвон <@tag>` — вызывает человека на обзвон\n"
        "┃\n"
        "┃ 📩 **Уведомление в ЛС**\n"
        "┃ `.call <@tag>` — напомнить ответить в заявке\n"
        "┃\n"
        "┃ 🩷 **Приватка**\n"
        "┃ `.privatkaform` — заново отправить форму ника\n"
        "┃\n"
        "┃ 🚫 **Доступ к заявкам**\n"
        "┃ `.block <@tag>` — заблокировать доступ к заявкам\n"
        "┃ `.unblock <@tag>` — разблокировать доступ к заявкам\n"
        "┃\n"
        "┃ 🛡️ **Защита каналов**\n"
        "┃ `.protect_on <id сервера>` — зафиксировать текущий порядок каналов\n"
        "┃ `.protect_off <id сервера>` — выключить защиту порядка каналов\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "**Статусы заявок:**\n"
        "> `🆕・user` — новая заявка\n"
        "> `🔵・user` — пользователь ждёт ответа\n"
        "> `🟡・user` — модератор ответил\n\n"
        "Все отчёты по рабочим командам отправляются в канал `логи-причин`."
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

    if (message.content or "").strip().lower() == ".privatkaform":
        if message.author.id != ADMIN_COMMANDS_ALLOWED_USER_ID:
            return True

        await _delete_command_message(message)
        ok = await ensure_private_setup_message(force_new=True)
        if ok:
            await _send(message.channel, "✅ **Форма ника для приватки отправлена заново.**")
        else:
            await _send(
                message.channel,
                "❌ **Не смог отправить форму ника.** Проверь ID канала приватки, права бота и доступ к каналу.",
            )
        return True


    # .protect_on/.protect_off — защита порядка каналов на выбранном сервере.
    protect_action, protect_guild_id = _parse_protect_command(message.content)
    if protect_action is not None:
        if message.author.id != ADMIN_COMMANDS_ALLOWED_USER_ID:
            return True

        await _delete_command_message(message)

        if protect_guild_id is None:
            await _send(message.channel, "**Использование:** `.protect_on <id сервера>` или `.protect_off <id сервера>`")
            return True

        if protect_guild_id not in PROTECTED_GUILD_LOG_CHANNELS:
            allowed = ", ".join(f"`{gid}`" for gid in PROTECTED_GUILD_LOG_CHANNELS)
            await _send(message.channel, f"❌ **Этот сервер не добавлен в защиту.** Доступные ID: {allowed}")
            return True

        guild = client.get_guild(protect_guild_id)
        if guild is None:
            await _send(message.channel, "❌ **Бот не видит этот сервер.** Проверь ID сервера и находится ли бот на нём.")
            return True

        try:
            enabled = protect_action == "protect_on"
            channel_count = set_protection_enabled(guild, enabled)
            await log_protection_command(guild, enabled=enabled, moderator=message.author, channel_count=channel_count)
            if enabled:
                await _send(message.channel, f"✅ **Защита включена** для сервера `{guild.name}` (`{guild.id}`). Зафиксировано каналов/категорий: `{channel_count}`.")
            else:
                await _send(message.channel, f"🔓 **Защита выключена** для сервера `{guild.name}` (`{guild.id}`).")
        except discord.Forbidden:
            await _send(message.channel, "❌ **Не хватает прав.** Нужны `Manage Channels` и желательно `View Audit Log` для определения нарушителя.")
        except Exception as e:
            await _send(message.channel, f"❌ **Не смог изменить защиту:** `{type(e).__name__}`")
            print(f"[ChannelProtection] protect command failed: {type(e).__name__}: {e}", flush=True)
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
        await send_report(
            client,
            message,
            _block_report(action, False, message.author.id, target_id, "Роль блокировки заявок не найдена на сервере."),
        )
        return True

    target = await _resolve_member(message, target_id)
    if target is None or getattr(target, "bot", False):
        await send_report(
            client,
            message,
            _block_report(action, False, message.author.id, target_id, "Я не смог найти этого пользователя на сервере."),
        )
        return True

    try:
        if action == "block":
            if role not in target.roles:
                await target.add_roles(role, reason=f"[SH] Application access blocked by {message.author} ({message.author.id})")
            await send_report(
                client,
                message,
                _block_report(action, True, message.author.id, target.id, "Пользователю выдана роль, блокирующая доступ к заявкам."),
            )
        else:
            if role in target.roles:
                await target.remove_roles(role, reason=f"[SH] Application access unblocked by {message.author} ({message.author.id})")
            await send_report(
                client,
                message,
                _block_report(action, True, message.author.id, target.id, "У пользователя забрана роль, блокирующая доступ к заявкам."),
            )
    except discord.Forbidden:
        await send_report(
            client,
            message,
            _block_report(action, False, message.author.id, target.id, "У меня нет прав выдать/забрать эту роль. Проверь права и позицию роли бота."),
        )
    except discord.HTTPException:
        await send_report(
            client,
            message,
            _block_report(action, False, message.author.id, target.id, "Discord не дал выполнить действие, попробуйте ещё раз."),
        )

    return True
