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
    PRIVATE_SETUP_TARGET_MESSAGE_ID,
)
from db import db_get_private_setup_message, db_set_private_setup_message, db_delete_private_setup_message


# ==========================================================
#                 PRIVATKA: NICKNAME SETUP
# ==========================================================

# Мягко-фиолетовый цвет embed под стиль сервера
PRIVATE_SETUP_EMBED_COLOR = 0xB58CFF


def build_private_setup_embed(guild: discord.Guild | None = None) -> discord.Embed:
    embed = discord.Embed(
        title="Приватка: установка ника",
        description=(
            "Нажми кнопку ниже и заполни короткую форму.\n"
            "После отправки бот автоматически поставит ник по формату "
            "**`Ник в стиме | Настоящее имя`** и обновит роли."
        ),
        color=PRIVATE_SETUP_EMBED_COLOR,
    )
    embed.add_field(
        name="Что нужно указать",
        value="• **Ваш ник в стиме**\n• **Ваше настоящее имя**",
        inline=False,
    )
    embed.add_field(
        name="Пример",
        value="`Famus x GOD | Дима`",
        inline=False,
    )
    embed.set_footer(text="SH Privatka • Nickname Setup")
    if guild and guild.icon:
        try:
            embed.set_thumbnail(url=guild.icon.url)
        except Exception:
            pass
    return embed


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

        # Discord требует ответ на interaction примерно за 3 секунды.
        # Смена ника/ролей может занять дольше, поэтому сразу подтверждаем форму,
        # а итог отправляем через followup. Это чинит Unknown interaction.
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException:
            pass

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

        try:
            await interaction.followup.send("\n".join(lines), ephemeral=True)
        except discord.HTTPException:
            pass


class PrivateSetupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Заполнить форму",
        emoji="📝",
        style=discord.ButtonStyle.success,
        custom_id="sh_private_open_form",
    )
    async def open_form(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or interaction.guild.id != PRIVATE_GUILD_ID:
            return await interaction.response.send_message("Эта кнопка работает только в приватке.", ephemeral=True)

        await interaction.response.send_modal(PrivateNicknameModal())


async def ensure_private_setup_message(*, force_new: bool = False) -> bool:
    """Создаёт/обновляет красивое сообщение с кнопкой формы в приватке.

    Если PRIVATE_SETUP_TARGET_MESSAGE_ID указан и сообщение принадлежит боту,
    бот редактирует именно его. Чужое пользовательское сообщение Discord API
    редактировать и снабжать кнопкой не позволяет.
    """
    try:
        ch = client.get_channel(PRIVATE_SETUP_CHANNEL_ID) or await client.fetch_channel(PRIVATE_SETUP_CHANNEL_ID)
    except discord.Forbidden:
        print(f"[Privatka] Нет доступа к каналу формы: {PRIVATE_SETUP_CHANNEL_ID}")
        return False
    except discord.NotFound:
        print(f"[Privatka] Канал формы не найден: {PRIVATE_SETUP_CHANNEL_ID}")
        return False
    except discord.HTTPException as e:
        print(f"[Privatka] Не смог получить канал формы {PRIVATE_SETUP_CHANNEL_ID}: {e}")
        return False

    if not isinstance(ch, discord.TextChannel):
        print(f"[Privatka] Канал формы {PRIVATE_SETUP_CHANNEL_ID} не является текстовым каналом")
        return False

    if ch.guild.id != PRIVATE_GUILD_ID:
        print(f"[Privatka] Канал формы находится не в приватке: guild={ch.guild.id}")
        return False

    embed = build_private_setup_embed(ch.guild)
    view = PrivateSetupView()

    # 1) Приоритет: конкретное сообщение, которое ты указал.
    target_id = int(PRIVATE_SETUP_TARGET_MESSAGE_ID or 0)
    if target_id and not force_new:
        try:
            target_msg = await ch.fetch_message(target_id)
            if client.user and target_msg.author and target_msg.author.id == client.user.id:
                await target_msg.edit(
                    content=None,
                    embed=embed,
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                db_set_private_setup_message(PRIVATE_SETUP_CHANNEL_ID, target_msg.id)
                print(f"[Privatka] Сообщение формы привязано к target message={target_msg.id}")
                return True
            else:
                print(
                    f"[Privatka] Нельзя привязать кнопку к сообщению {target_id}: "
                    "Discord разрешает редактировать только сообщения самого бота."
                )
        except discord.NotFound:
            print(f"[Privatka] Target message не найден: {target_id}")
        except discord.Forbidden:
            print(f"[Privatka] Нет прав читать/редактировать target message: {target_id}")
            return False
        except discord.HTTPException as e:
            print(f"[Privatka] Не смог обновить target message {target_id}: {e}")
            return False

    # 2) Если target не подошёл — обновляем сохранённое сообщение бота.
    if not force_new:
        stored_id = db_get_private_setup_message(PRIVATE_SETUP_CHANNEL_ID)
        if stored_id:
            try:
                old_msg = await ch.fetch_message(stored_id)
                if client.user and old_msg.author and old_msg.author.id == client.user.id:
                    await old_msg.edit(
                        content=None,
                        embed=embed,
                        view=view,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    print(f"[Privatka] Сообщение формы уже есть и обновлено: {old_msg.id}")
                    return True
            except discord.NotFound:
                db_delete_private_setup_message(PRIVATE_SETUP_CHANNEL_ID)
            except discord.Forbidden:
                print(f"[Privatka] Нет прав читать/редактировать старое сообщение формы в канале {PRIVATE_SETUP_CHANNEL_ID}")
                return False
            except discord.HTTPException as e:
                print(f"[Privatka] Не смог проверить/обновить старое сообщение формы: {e}")
                return False

    # 3) Запасной вариант — отправляем новое красивое сообщение.
    try:
        msg = await ch.send(
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        db_set_private_setup_message(PRIVATE_SETUP_CHANNEL_ID, msg.id)
        print(f"[Privatka] Сообщение формы отправлено: channel={PRIVATE_SETUP_CHANNEL_ID} message={msg.id}")
        return True
    except discord.Forbidden:
        print(f"[Privatka] Нет прав отправлять сообщения в канал формы: {PRIVATE_SETUP_CHANNEL_ID}")
    except discord.HTTPException as e:
        print(f"[Privatka] Не смог отправить сообщение формы: {e}")

        # fallback на простой текст, если Discord почему-то не принял embed
        try:
            msg = await ch.send(
                PRIVATE_SETUP_MESSAGE,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            db_set_private_setup_message(PRIVATE_SETUP_CHANNEL_ID, msg.id)
            return True
        except Exception:
            pass
    return False


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
