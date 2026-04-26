# voice_call_command.py
"""
Команда для модераторов, которая зовёт пользователя на обзвон.

Доступна только участникам с ролью "Модератор".
Формат:
.vc <@user>
.vc <user_id>
.obzvon <@user>
.obzvon <user_id>
.обзвон <@user>
.обзвон <user_id>

Бот:
1) удаляет сообщение с командой;
2) отправляет пользователю приглашение на обзвон в текущий канал;
3) отправляет красивый отчёт в канал логов причин.
"""

from __future__ import annotations

import re
import discord

from command_reports import build_report, send_report

MODERATOR_ROLE_ID = 1364549372313993216
VOICE_CALL_LOG_CHANNEL_ID = 1466163549150773363
VOICE_WAITING_INVITE_URL = "https://discord.gg/ArwkTVt6ty"

_VOICE_CALL_RE = re.compile(
    r"^\s*(\.vc|\.obzvon|\.обзвон)\s+(?:<@!?(\d{15,25})>|(\d{15,25}))\s*$",
    re.IGNORECASE,
)


def is_voice_call_command(content: str | None) -> bool:
    if not content:
        return False
    lowered = content.strip().lower()
    return lowered.startswith(".vc") or lowered.startswith(".obzvon") or lowered.startswith(".обзвон")


def _extract_command_and_target_id(content: str | None) -> tuple[str | None, int | None]:
    match = _VOICE_CALL_RE.match(content or "")
    if not match:
        return None, None

    command = (match.group(1) or "").lower()
    raw = match.group(2) or match.group(3)
    if not raw:
        return command, None

    try:
        return command, int(raw)
    except ValueError:
        return command, None


def _has_moderator_role(member: discord.Member) -> bool:
    return any(role.id == MODERATOR_ROLE_ID for role in member.roles)


async def _delete_command_message(message: discord.Message) -> None:
    try:
        await message.delete()
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        pass


async def _get_log_channel(client: discord.Client, message: discord.Message) -> discord.abc.Messageable | None:
    """Ищем канал логов причин по ID."""
    if message.guild:
        channel = message.guild.get_channel(VOICE_CALL_LOG_CHANNEL_ID)
        if channel:
            return channel

    channel = client.get_channel(VOICE_CALL_LOG_CHANNEL_ID)
    if channel:
        return channel

    try:
        return await client.fetch_channel(VOICE_CALL_LOG_CHANNEL_ID)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


async def _send_voice_call_report(
    client: discord.Client,
    message: discord.Message,
    ok: bool,
    target_id: int | None,
    command: str | None,
) -> None:
    cmd = command or "не распознана"
    title = "✅ **・Приглашение на обзвон отправлено**" if ok else "❌ **・Приглашение на обзвон не отправлено**"
    details = (
        f"Пользователь был вызван на проверку. Канал: **[ожидание проверки]({VOICE_WAITING_INVITE_URL})**."
        if ok
        else "Не получилось отправить приглашение. Проверьте упоминание пользователя, права бота или доступность канала."
    )
    text = build_report(title, message.author.id, target_id, cmd, ok, details)
    await send_report(client, message, text)


async def _send_usage(channel: discord.abc.Messageable) -> None:
    try:
        await channel.send(
            "**Использование:** `.vc @пользователь`, `.obzvon @пользователь` или `.обзвон @пользователь`",
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        pass


async def handle_voice_call_command(client: discord.Client, message: discord.Message) -> bool:
    """
    Возвращает True, если сообщение было командой вызова на обзвон.
    Возвращает False, если это не эта команда.
    """
    if not is_voice_call_command(message.content):
        return False

    # Команда работает только на сервере и только для участников с ролью Модератор.
    if not message.guild or not isinstance(message.author, discord.Member):
        return True

    if not _has_moderator_role(message.author):
        return True

    command, target_id = _extract_command_and_target_id(message.content)

    # Удаляем команду, чтобы в канале осталось только красивое сообщение от бота.
    await _delete_command_message(message)

    if target_id is None:
        await _send_usage(message.channel)
        await _send_voice_call_report(client, message, False, None, command)
        return True

    text = (
        f"<@{target_id}>, вас вызывает модератор на обзвон. "
        f"Перейдите, пожалуйста, в канал **[ожидание проверки]({VOICE_WAITING_INVITE_URL})**."
    )

    ok = False
    try:
        sent = await message.channel.send(
            text,
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            suppress_embeds=True,
        )
        ok = True

        # Убираем Discord invite/link preview, чтобы не появлялась большая карточка Discord.
        try:
            await sent.edit(suppress=True)
        except (discord.Forbidden, discord.HTTPException, TypeError):
            try:
                await sent.suppress_embeds()
            except (discord.Forbidden, discord.HTTPException, AttributeError):
                pass
    except discord.HTTPException:
        ok = False

    await _send_voice_call_report(client, message, ok, target_id, command)
    return True
