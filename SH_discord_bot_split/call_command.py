# call_command.py
"""
Команда .call для ручного вызова пользователя в ЛС.

Доступна только пользователю с ID CALL_COMMAND_ALLOWED_USER_ID.
Формат:
.call <@user>
.call <user_id>

После команды бот:
1) удаляет сообщение с командой;
2) отправляет указанному человеку уведомление в ЛС;
3) пишет в текущий канал результат: успешно / неуспешно.
"""

from __future__ import annotations

import re
import discord

CALL_COMMAND_ALLOWED_USER_ID = 1105559182624694393

_CALL_RE = re.compile(r"^\s*\.call\s+(?:<@!?(\d{15,25})>|(\d{15,25}))\s*$", re.IGNORECASE)


def is_call_command(content: str | None) -> bool:
    return bool(content and content.strip().lower().startswith(".call"))


def _extract_target_id(content: str | None) -> int | None:
    match = _CALL_RE.match(content or "")
    if not match:
        return None

    raw = match.group(1) or match.group(2)
    if not raw:
        return None

    try:
        return int(raw)
    except ValueError:
        return None


async def _delete_command_message(message: discord.Message) -> None:
    try:
        await message.delete()
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        pass


async def _send_result(channel: discord.abc.Messageable, ok: bool) -> None:
    text = (
        "**Успешно. Я отправил уведомление пользователю.**"
        if ok
        else "**Неуспешно. У меня почему-то не получилось отправить уведомление пользователю.**"
    )
    try:
        await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        pass


async def _resolve_target_user(client: discord.Client, message: discord.Message, user_id: int) -> discord.abc.User | None:
    # Если пользователь упомянут в сообщении — берём его сразу.
    for mentioned in getattr(message, "mentions", []) or []:
        if mentioned.id == user_id:
            return mentioned

    # Если пользователь есть на сервере — берём Member.
    if message.guild:
        member = message.guild.get_member(user_id)
        if member:
            return member
        try:
            member = await message.guild.fetch_member(user_id)
            if member:
                return member
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    # Фоллбек: обычный User по ID.
    try:
        return await client.fetch_user(user_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


async def handle_call_command(client: discord.Client, message: discord.Message) -> bool:
    """
    Возвращает True, если сообщение было командой .call и обработчик должен остановиться.
    Возвращает False, если это не .call.
    """
    if not is_call_command(message.content):
        return False

    # Команду разрешаем только конкретному пользователю.
    if not message.author or message.author.id != CALL_COMMAND_ALLOWED_USER_ID:
        return True

    await _delete_command_message(message)

    # Команда нужна именно в текстовом канале сервера, чтобы дать ссылку на канал.
    if not message.guild or not isinstance(message.channel, discord.TextChannel):
        await _send_result(message.channel, False)
        return True

    target_id = _extract_target_id(message.content)
    if target_id is None:
        await _send_result(message.channel, False)
        return True

    target = await _resolve_target_user(client, message, target_id)
    if target is None or getattr(target, "bot", False):
        await _send_result(message.channel, False)
        return True

    dm_text = (
        "**Уведомление!**\n\n"
        "*Приветствую. Вы недавно создавали заявку в клан, но из-за вашей неактивности "
        "модератор отправляет вам напоминание в личные сообщения.*\n\n"
        f"**Ссылка на ваш текстовый канал:** {message.channel.jump_url}\n\n"
        "*Пожалуйста, ответьте в заявке. Если ответа не будет, ваша заявка может быть закрыта.*"
    )

    ok = False
    try:
        await target.send(dm_text, allowed_mentions=discord.AllowedMentions.none())
        ok = True
    except (discord.Forbidden, discord.HTTPException):
        ok = False

    await _send_result(message.channel, ok)
    return True
