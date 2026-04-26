# privatka.py
import re
import time
import discord

from app import client
from config import (
    PRIVATE_GUILD_ID,
    PRIVATE_SETUP_CHANNEL_ID,
    PRIVATE_REMOVE_ROLE_ID,
    PRIVATE_ADD_ROLE_ID,
    PRIVATE_SETUP_MESSAGE,
)
from db import db_get_private_setup_message, db_set_private_setup_message


# ==========================================================
#                 PRIVATKA: NICKNAME SETUP
# ==========================================================

def _clean_one_line(value: str) -> str:
    value = value.replace("\n", " ").replace("\r", " ").strip()
    value = re.sub(r"\s+", " ", value)
    return value


def _smart_title_case(value: str) -> str:
    """Приводит строку к 'Title Case' только если пользователь ввёл всё с маленькой буквы.
    Это защищает уже корректные никнеймы вроде 'Famus x GOD' от порчи.
    """
    value = _clean_one_line(value)
    if not value:
        return value

    has_upper = any(ch.isalpha() and ch.isupper() for ch in value)
    if has_upper:
        return value  # оставляем как есть

    # если нет заглавных букв — считаем ввод "нижним регистром" и форматируем
    return value.title()



def format_private_nickname(steam_nick: str, real_name: str, *, max_len: int = 32) -> str:
    # Формат: "SteamNick | RealName"
    # Discord ограничивает nick 32 символами — аккуратно режем, если нужно.
    steam_nick = _smart_title_case(steam_nick)
    real_name = _smart_title_case(real_name)

    sep = " | "
    full = f"{steam_nick}{sep}{real_name}"

    if len(full) <= max_len:
        return full

    # сначала подрежем steam_nick, сохранив real_name максимально
    available_for_steam = max_len - len(sep) - len(real_name)
    if available_for_steam < 1:
        # real_name слишком длинное — режем его, чтобы осталось место хотя бы на 1 символ steam
        real_name = real_name[: max(1, max_len - len(sep) - 1)]
        available_for_steam = 1

    steam_nick = steam_nick[:available_for_steam]
    full = f"{steam_nick}{sep}{real_name}"
    return full[:max_len]


class PrivateNicknameModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Приватка — установка ника")

        self.steam_nick = discord.ui.TextInput(
            label="Ваш ник в стиме",
            placeholder="Например: Famus x GOD",
            style=discord.TextStyle.short,
            required=True,
            max_length=64,
        )
        self.real_name = discord.ui.TextInput(
            label="Ваше настоящее имя",
            placeholder="Например: Дима",
            style=discord.TextStyle.short,
            required=True,
            max_length=64,
        )
        self.add_item(self.steam_nick)
        self.add_item(self.real_name)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Ошибка: не удалось определить сервер/участника.", ephemeral=True)

        # строго работаем только в приватке
        if interaction.guild.id != PRIVATE_GUILD_ID:
            return await interaction.response.send_message("Эта форма работает только в приватке.", ephemeral=True)

        member: discord.Member = interaction.user
        new_nick = format_private_nickname(self.steam_nick.value, self.real_name.value)

        nick_ok = True
        roles_ok = True
        nick_err = ""
        roles_err = ""

        # 1) меняем ник
        try:
            await member.edit(nick=new_nick, reason="[SH] Privatka nickname setup")
        except discord.Forbidden:
            nick_ok = False
            nick_err = "Нет прав на изменение ника (Manage Nicknames) или роль бота ниже."
        except discord.HTTPException:
            nick_ok = False
            nick_err = "Не удалось изменить ник (ошибка Discord)."

        # 2) роли
        remove_role = interaction.guild.get_role(PRIVATE_REMOVE_ROLE_ID)
        add_role = interaction.guild.get_role(PRIVATE_ADD_ROLE_ID)
        try:
            if remove_role and remove_role in member.roles:
                await member.remove_roles(remove_role, reason="[SH] Privatka nickname setup: remove role")
            if add_role and add_role not in member.roles:
                await member.add_roles(add_role, reason="[SH] Privatka nickname setup: add role")
        except discord.Forbidden:
            roles_ok = False
            roles_err = "Нет прав на выдачу ролей (Manage Roles) или роли выше роли бота."
        except discord.HTTPException:
            roles_ok = False
            roles_err = "Не удалось обновить роли (ошибка Discord)."

        # ответ пользователю
        lines = []
        if nick_ok:
            lines.append(f"✅ Ник установлен: **{new_nick}**")
        else:
            lines.append(f"❌ Ник не изменён. {nick_err}")

        if roles_ok:
            lines.append("✅ Роли обновлены.")
        else:
            lines.append(f"❌ Роли не обновлены. {roles_err}")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)


class PrivateSetupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Заполнить форму",
        style=discord.ButtonStyle.primary,
        custom_id="sh_private_open_form",
    )
    async def open_form(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or interaction.guild.id != PRIVATE_GUILD_ID:
            return await interaction.response.send_message("Эта кнопка работает только в приватке.", ephemeral=True)

        await interaction.response.send_modal(PrivateNicknameModal())


async def ensure_private_setup_message() -> None:
    # Создаёт (один раз) сообщение с кнопкой в канале приватки.
    try:
        ch = client.get_channel(PRIVATE_SETUP_CHANNEL_ID) or await client.fetch_channel(PRIVATE_SETUP_CHANNEL_ID)
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        return

    if not isinstance(ch, discord.TextChannel):
        return

    if ch.guild.id != PRIVATE_GUILD_ID:
        return

    stored_id = db_get_private_setup_message(PRIVATE_SETUP_CHANNEL_ID)
    if stored_id:
        try:
            old = await ch.fetch_message(stored_id)
            if client.user and old.author and old.author.id == client.user.id:
                return  # уже есть
        except (discord.NotFound, discord.HTTPException):
            pass

    try:
        msg = await ch.send(
            PRIVATE_SETUP_MESSAGE,
            view=PrivateSetupView(),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        db_set_private_setup_message(PRIVATE_SETUP_CHANNEL_ID, msg.id)
    except discord.HTTPException:
        return


# ==========================================================
#                PRIVATKA: INVITE GENERATION
# ==========================================================

async def create_one_time_private_invite(
    *,
    opener: discord.abc.User,
    moderator: discord.Member | discord.User,
) -> discord.Invite | None:
    """Создаёт персональный инвайт в приватку (1 день, 1 использование)."""
    from config import (
        PRIVATE_GUILD_ID,
        PRIVATE_SETUP_CHANNEL_ID,
        PRIVATE_INVITE_MAX_AGE_SECONDS,
        PRIVATE_INVITE_MAX_USES,
    )
    from db import db_log_invite

    guild = client.get_guild(PRIVATE_GUILD_ID)
    if guild is None:
        return None

    # Пробуем создать инвайт в заданном канале, иначе ищем первый доступный текстовый канал
    target_channel = guild.get_channel(PRIVATE_SETUP_CHANNEL_ID)
    invite_channel: discord.abc.GuildChannel | None = None

    me = guild.get_member(client.user.id) if client.user else None

    def _can_create(ch: discord.abc.GuildChannel) -> bool:
        if not me:
            return True
        perms = ch.permissions_for(me)
        return perms.create_instant_invite and perms.view_channel

    if isinstance(target_channel, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel)):
        if _can_create(target_channel):
            invite_channel = target_channel

    if invite_channel is None:
        # fallback: любой доступный текстовый/голосовой канал
        for ch in list(guild.text_channels) + list(guild.voice_channels) + list(getattr(guild, "stage_channels", [])):
            try:
                if _can_create(ch):
                    invite_channel = ch
                    break
            except Exception:
                continue

    if invite_channel is None:
        return None

    try:
        invite = await invite_channel.create_invite(
            max_age=PRIVATE_INVITE_MAX_AGE_SECONDS,
            max_uses=PRIVATE_INVITE_MAX_USES,
            unique=True,
            reason=f"[SH] one-time privatka invite for user {opener.id} by {getattr(moderator, 'id', 0)}",
        )
        expires_at = int(time.time()) + int(PRIVATE_INVITE_MAX_AGE_SECONDS)
        try:
            db_log_invite(invite.code, opener.id, getattr(moderator, "id", 0), invite_channel.id, expires_at)
        except Exception:
            pass
        return invite
    except (discord.Forbidden, discord.HTTPException):
        return None
