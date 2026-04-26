from __future__ import annotations

import discord

COMMAND_LOG_CHANNEL_ID = 1466163549150773363


async def get_log_channel(client: discord.Client, message: discord.Message) -> discord.abc.Messageable | None:
    if message.guild:
        channel = message.guild.get_channel(COMMAND_LOG_CHANNEL_ID)
        if channel:
            return channel

    channel = client.get_channel(COMMAND_LOG_CHANNEL_ID)
    if channel:
        return channel

    try:
        return await client.fetch_channel(COMMAND_LOG_CHANNEL_ID)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


def build_report(title: str, moderator_id: int, target_id: int | None, command: str, ok: bool, details: str) -> str:
    target_text = f"<@{target_id}>" if target_id else "не указан"
    status = "🟢 Успешно" if ok else "🔴 Не успешно"
    return (
        f"{title}\n"
        f"╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
        f"┃ 👮 **Модератор:** <@{moderator_id}>\n"
        f"┃ 👤 **Пользователь:** {target_text}\n"
        f"┃ ⚙️ **Команда:** `{command}`\n"
        f"┃ 📌 **Статус:** {status}\n"
        f"╰━━━━━━━━━━━━━━━━━━━━━━╯\n"
        f"> {details}"
    )


async def send_report(client: discord.Client, message: discord.Message, text: str) -> None:
    channel = await get_log_channel(client, message)
    if channel is None:
        channel = message.channel

    sent: discord.Message | None = None

    try:
        # Убирает огромные Discord preview-карточки от invite-ссылок в логах.
        sent = await channel.send(
            text,
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            suppress_embeds=True,
        )
    except TypeError:
        # Фоллбек для старой discord.py, где нет suppress_embeds в send().
        try:
            sent = await channel.send(
                text,
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
        except discord.HTTPException:
            return
    except discord.HTTPException:
        return

    if sent is not None:
        try:
            await sent.edit(suppress=True)
        except (discord.Forbidden, discord.HTTPException, TypeError):
            try:
                await sent.suppress_embeds()
            except (discord.Forbidden, discord.HTTPException, AttributeError):
                pass
