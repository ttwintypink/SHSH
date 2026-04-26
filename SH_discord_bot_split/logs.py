# logs.py
import discord

from config import LOG_CHANNEL_ID


async def log_event(guild: discord.Guild, text: str) -> None:
    if not LOG_CHANNEL_ID:
        return
    ch = guild.get_channel(LOG_CHANNEL_ID)
    if isinstance(ch, discord.TextChannel):
        try:
            await ch.send(text, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass


async def send_application_log(
    guild: discord.Guild,
    *,
    decision: str,
    opener: discord.abc.User | None,
    moderator: discord.Member,
    reason_text: str,
    dm_sent: bool,
) -> None:
    """Отправляет в лог-канал сообщение по шаблону + ставит реакцию."""
    if not LOG_CHANNEL_ID:
        return

    ch = guild.get_channel(LOG_CHANNEL_ID)
    if not isinstance(ch, discord.TextChannel):
        return

    player_part = opener.mention if opener else "*(не удалось определить игрока)*"
    mod_part = moderator.mention
    dm_part = "**YES**" if dm_sent else "**NO**"

    if decision == "accept":
        header = "**✅ Заявка была успешно принята.**"
        body = (
            f"{header}\n"
            f"*Был принят игрок:* {player_part}\n"
            f"*Заявка была принята модератором:* {mod_part}\n"
            f"*Заявка была принята с причиной:* **{reason_text}**\n\n"
            f"*Отправилось ли сообщение в dm игроку?*: {dm_part}"
        )
        reaction = "✅"
    else:
        header = "**❌ Заявка была успешно отклонена.**"
        body = (
            f"{header}\n"
            f"*Был отклонен игрок:* {player_part}\n"
            f"*Заявка была отклонена модератором:* {mod_part}\n"
            f"*Заявка была отклонена с причиной:* **{reason_text}**\n\n"
            f"*Отправилось ли сообщение в dm игроку?*: {dm_part}"
        )
        reaction = "❌"

    try:
        msg = await ch.send(
            body,
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )
    except discord.HTTPException:
        return

    try:
        await msg.add_reaction(reaction)
    except (discord.Forbidden, discord.HTTPException):
        pass
