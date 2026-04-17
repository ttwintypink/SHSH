# SH.py (оригинальная монолитная версия)
# Токен берётся из ENV (как и в split-версии). Файл .env не используется.

import os
import re
import time
import sqlite3
import asyncio
import discord


# ==========================================================
#                       CONFIG
# ==========================================================

def _clean_token(raw: str) -> str:
    v = raw.strip()
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        v = v[1:-1].strip()
    return v


def _first_env(*names: str) -> tuple[str | None, str | None]:
    for n in names:
        val = os.getenv(n)
        if val and val.strip():
            return _clean_token(val), n
    return None, None


TOKEN, _TOKEN_ENV_NAME = _first_env(
    "DISCORD_TOKEN",
    "TOKEN",
    "BOT_TOKEN",
    "DISCORD_BOT_TOKEN",
)

if not TOKEN:
    raise RuntimeError(
        "Токен бота не найден в переменных окружения.\n"
        "Задай одну из переменных (ENV): DISCORD_TOKEN / TOKEN / BOT_TOKEN / DISCORD_BOT_TOKEN\n"
    )

print(f"✅ TOKEN загружен из ENV: {_TOKEN_ENV_NAME}")

# Категория, где Ticket Tool создаёт тикеты
TICKETS_CATEGORY_ID = 1371053315529637928

# Категория архива
ARCHIVE_CATEGORY_ID = 1287197183954784297

# Роли модераторов: имеют право принимать/отклонять + будут пинговаться
STAFF_ROLE_IDS: list[int] = [
    1300451930900009092,
    1364660029143253082,
    1371955005367779418,
    1320456192568459265,
    1320438406370824305,
    1364549372313993216,
    1349429359290876037,
]

# Роли, которые пингуем (оставляем как было раньше)
STAFF_PING_ROLE_IDS: list[int] = [
    1300451930900009092,
    1364660029143253082,
    1364549372313993216,
]

# (опционально) канал логов (0 = выключено)
LOG_CHANNEL_ID = 1466163549150773363

# Фраза-триггер (Ticket Tool пишет это при попытке закрыть тикет)
TRIGGER_PHRASE = "вы серьезно хотите закрыть данный тикет"

# Анти-спам: не чаще, чем раз в N секунд в одном канале
PROMPT_COOLDOWN_SECONDS = 30

# SQLite
DB_PATH = "tickets.db"

# Инвайт (permanent link)
INVITE_LINK = "https://discord.gg/Pgs8uZffhr"

# Роли для результата заявки
ACCEPT_ADD_ROLE_ID = 1299444337658171422      # выдать при принятии
ACCEPT_REMOVE_ROLE_ID = 1315028367044513876   # забрать при принятии

ACCEPT_EXTRA_DM = """**Так же не забудьте поставить ник по форме в приватке, и добавить приписку в стим профиле!**
*Форма для стима: SH | nick*
*Форма для приватки: Ник | Имя*"""

WELCOME_MESSAGE = """**Шаблон для подачи заявки в клан:** 

```1) Возраст, имя, кол-во часов (на одном аккаунте, пиратки не считаются)
2) Ваши преимущества
3) Роль в клане
4) Играете ли вы час в день на юкн?
5) Был ли опыт в кланах? (если да то в каких)
6) Готовы вы ли вы покупать випки?
7) Профиль стим (ТРЕБУЕТСЯ открытый стим аккаунт)
8) Ваш часовой пояс
9) Сколько играете в день
10) Откуда узнали о нас?
11) Ваша характеристика пк (проц, видюха, сколько фпса)
12) Принимаете ли вы обоснованную критику в свою сторону?
13) Готовы пройти проверку?
14) 40 UKN walls - 45 R2 fc```

***Заполняйте свою заявку по форме выше!***"""

# -------------------- PRIVATKA SETUP --------------------
# Приватный сервер (гильдия) и канал, где будет сообщение с кнопкой формы
PRIVATE_GUILD_ID = 1454836789331230842
PRIVATE_SETUP_CHANNEL_ID = 1466168457757462741

# Роли в приватке:
# - роль, которую нужно снять после заполнения формы
# - роль, которую нужно выдать после заполнения формы
PRIVATE_REMOVE_ROLE_ID = 1466169062491951164
PRIVATE_ADD_ROLE_ID = 1454842309421170719

# Текст сообщения с кнопкой (можешь поменять позже)
PRIVATE_SETUP_MESSAGE = (
    "**Приватка: установка ника**\n"
    "Нажми кнопку ниже, заполни форму — и я автоматически поставлю тебе ник по формату "
    "**`Ник в стиме | Настоящее имя`** и обновлю роли."
)


# ==========================================================
#                      INTENTS / CLIENT
# ==========================================================

intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.message_content = True  # включить в Dev Portal -> Privileged Gateway Intents

client = discord.Client(intents=intents)

_last_prompt_time: dict[int, float] = {}
_channel_locks: dict[int, asyncio.Lock] = {}


def _get_channel_lock(channel_id: int) -> asyncio.Lock:
    lock = _channel_locks.get(channel_id)
    if lock is None:
        lock = asyncio.Lock()
        _channel_locks[channel_id] = lock
    return lock


# ==========================================================
#                      DB HELPERS
# ==========================================================

def db_init() -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS tickets ("
            "channel_id INTEGER PRIMARY KEY, "
            "opener_id INTEGER NOT NULL, "
            "created_at INTEGER NOT NULL"
            ");"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS prompts ("
            "channel_id INTEGER PRIMARY KEY, "
            "prompt_message_id INTEGER NOT NULL, "
            "created_at INTEGER NOT NULL"
            ");"
        )

        con.execute(
            "CREATE TABLE IF NOT EXISTS private_setup ("
            "channel_id INTEGER PRIMARY KEY, "
            "message_id INTEGER NOT NULL, "
            "created_at INTEGER NOT NULL"
            ");"
        )


def db_set_opener(channel_id: int, opener_id: int) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT INTO tickets(channel_id, opener_id, created_at) VALUES(?, ?, ?) "
            "ON CONFLICT(channel_id) DO UPDATE SET opener_id=excluded.opener_id, created_at=excluded.created_at;",
            (channel_id, opener_id, int(time.time())),
        )


def db_get_opener(channel_id: int) -> int | None:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT opener_id FROM tickets WHERE channel_id=?;",
            (channel_id,),
        ).fetchone()
    return int(row[0]) if row else None


def db_delete_ticket(channel_id: int) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute("DELETE FROM tickets WHERE channel_id=?;", (channel_id,))


def db_set_prompt(channel_id: int, message_id: int) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT INTO prompts(channel_id, prompt_message_id, created_at) VALUES(?, ?, ?) "
            "ON CONFLICT(channel_id) DO UPDATE SET prompt_message_id=excluded.prompt_message_id, created_at=excluded.created_at;",
            (channel_id, message_id, int(time.time())),
        )


def db_get_prompt(channel_id: int) -> int | None:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT prompt_message_id FROM prompts WHERE channel_id=?;",
            (channel_id,),
        ).fetchone()
    return int(row[0]) if row else None


def db_delete_prompt(channel_id: int) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute("DELETE FROM prompts WHERE channel_id=?;", (channel_id,))


def db_set_private_setup_message(channel_id: int, message_id: int) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT INTO private_setup(channel_id, message_id, created_at) VALUES(?, ?, ?) "
            "ON CONFLICT(channel_id) DO UPDATE SET message_id=excluded.message_id, created_at=excluded.created_at;",
            (channel_id, message_id, int(time.time())),
        )


def db_get_private_setup_message(channel_id: int) -> int | None:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT message_id FROM private_setup WHERE channel_id=?;",
            (channel_id,),
        ).fetchone()
    return int(row[0]) if row else None


def db_delete_private_setup_message(channel_id: int) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute("DELETE FROM private_setup WHERE channel_id=?;", (channel_id,))



# ==========================================================
#                    HELPER FUNCTIONS
# ==========================================================

def is_staff(member: discord.Member) -> bool:
    # Жёстко по ролям + админ
    if member.guild_permissions.administrator:
        return True
    return any(r.id in STAFF_ROLE_IDS for r in member.roles)


def _normalize_text(text: str) -> str:
    text = text.lower().strip()
    text = text.replace("**", "").replace("__", "").replace("*", "").replace("`", "")
    text = re.sub(r"\s+", " ", text)
    return text.strip(" .,!?:;—-")


def message_contains_trigger(msg: discord.Message) -> bool:
    parts: list[str] = []
    if msg.content:
        parts.append(msg.content)

    for emb in msg.embeds:
        if emb.title:
            parts.append(emb.title)
        if emb.description:
            parts.append(emb.description)
        for f in emb.fields:
            if f.name:
                parts.append(f.name)
            if f.value:
                parts.append(f.value)
        if emb.footer and emb.footer.text:
            parts.append(emb.footer.text)

    joined = " ".join(parts)
    return TRIGGER_PHRASE in _normalize_text(joined)


def build_staff_ping(guild: discord.Guild) -> str:
    mentions = []
    for rid in STAFF_PING_ROLE_IDS:
        role = guild.get_role(rid)
        mentions.append(role.mention if role else f"<@&{rid}>")
    return " ".join(mentions).strip()


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


# ==========================================================
#                 PRIVATKA: NICKNAME SETUP
# ==========================================================

def _clean_one_line(value: str) -> str:
    value = value.replace("\n", " ").replace("\r", " ").strip()
    value = re.sub(r"\s+", " ", value)
    return value


def format_private_nickname(steam_nick: str, real_name: str, *, max_len: int = 32) -> str:
    # Формат: "SteamNick | RealName"
    # Discord ограничивает nick 32 символами — аккуратно режем, если нужно.
    steam_nick = _clean_one_line(steam_nick)
    real_name = _clean_one_line(real_name)

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
            placeholder="Например: SH | Nick",
            style=discord.TextStyle.short,
            required=True,
            max_length=64,
        )
        self.real_name = discord.ui.TextInput(
            label="Ваше настоящее имя",
            placeholder="Например: Иван",
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
async def resolve_ticket_opener_fallback(channel: discord.TextChannel) -> discord.abc.User | None:
    # topic
    if channel.topic:
        m = re.search(r"<@!?(\d{15,25})>", channel.topic)
        if m:
            uid = int(m.group(1))
            return channel.guild.get_member(uid) or await client.fetch_user(uid)

        m = re.search(r"\b(\d{15,25})\b", channel.topic)
        if m:
            uid = int(m.group(1))
            return channel.guild.get_member(uid) or await client.fetch_user(uid)

    # overwrites
    for target, ow in channel.overwrites.items():
        if isinstance(target, discord.Member) and not target.bot and ow.view_channel is True:
            return target

    # history
    try:
        async for m in channel.history(limit=200, oldest_first=True):
            if m.author and not m.author.bot:
                return m.author
    except discord.HTTPException:
        pass

    return None


async def get_opener_user(channel: discord.TextChannel) -> discord.abc.User | None:
    opener_id = db_get_opener(channel.id)
    if opener_id:
        member = channel.guild.get_member(opener_id)
        if member:
            return member
        try:
            return await client.fetch_user(opener_id)
        except discord.HTTPException:
            return None

    opener = await resolve_ticket_opener_fallback(channel)
    if opener:
        db_set_opener(channel.id, opener.id)
    return opener

async def ensure_guild_member(guild: discord.Guild, user_id: int) -> discord.Member | None:
    """
    Возвращает Member для user_id.
    1) пробуем из кеша
    2) если нет — делаем REST fetch (работает даже без intents.members)
    """
    m = guild.get_member(user_id)
    if m:
        return m
    try:
        return await guild.fetch_member(user_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


async def apply_accept_roles(
    guild: discord.Guild,
    opener_id: int,
    *,
    add_role_id: int,
    remove_role_id: int,
) -> tuple[bool, str]:
    """
    При принятии:
    - выдаём роль add_role_id
    - снимаем роль remove_role_id
    """
    member = await ensure_guild_member(guild, opener_id)
    if not member:
        return False, "member_not_found"

    add_role = guild.get_role(add_role_id)
    rem_role = guild.get_role(remove_role_id)

    if add_role is None and rem_role is None:
        return False, "roles_not_found"

    try:
        if rem_role and rem_role in member.roles:
            await member.remove_roles(rem_role, reason="[SH] Ticket accepted: remove role")
        if add_role and add_role not in member.roles:
            await member.add_roles(add_role, reason="[SH] Ticket accepted: add role")
        return True, "ok"
    except discord.Forbidden:
        # Обычно: у бота нет Manage Roles или роль выше роли бота
        return False, "forbidden_manage_roles_or_hierarchy"
    except discord.HTTPException:
        return False, "http_exception"


async def disable_or_delete_prompt_message(channel: discord.TextChannel) -> None:
    """
    Убираем возможность повторных нажатий:
    - пытаемся удалить сообщение с кнопками
    - если не получилось: снимаем кнопки (view=None) и меняем текст на "Закрыто."
    """
    msg_id = db_get_prompt(channel.id)
    if not msg_id:
        return

    try:
        msg = await channel.fetch_message(msg_id)
    except discord.NotFound:
        db_delete_prompt(channel.id)
        return
    except discord.HTTPException:
        return

    # delete
    try:
        await msg.delete()
        db_delete_prompt(channel.id)
        return
    except (discord.Forbidden, discord.HTTPException):
        pass

    # disable buttons
    try:
        await msg.edit(content="**Закрыто.**", view=None, allowed_mentions=discord.AllowedMentions.none())
        db_delete_prompt(channel.id)
    except discord.HTTPException:
        pass


async def archive_and_lock_channel(
    channel: discord.TextChannel,
    opener: discord.abc.User | None,
    moderator: discord.Member,
    reason_text: str,
) -> None:
    guild = channel.guild
    archive_category = guild.get_channel(ARCHIVE_CATEGORY_ID)
    if not isinstance(archive_category, discord.CategoryChannel):
        raise RuntimeError("ARCHIVE_CATEGORY_ID не найден или не является категорией.")

    overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {}

    # закрыть @everyone
    overwrites[guild.default_role] = discord.PermissionOverwrite(
        view_channel=False,
        send_messages=False,
        read_message_history=False,
    )

    # закрыть игрока
    if opener:
        opener_member = guild.get_member(opener.id)
        if opener_member:
            overwrites[opener_member] = discord.PermissionOverwrite(view_channel=False)

    # открыть модерам
    for rid in STAFF_PING_ROLE_IDS:
        role = guild.get_role(rid)
        if role:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
            )

    # открыть боту
    me = guild.get_member(client.user.id) if client.user else None
    if me:
        overwrites[me] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_channels=True,
        )

    await channel.edit(
        category=archive_category,
        overwrites=overwrites,
        reason=f"Archived by {moderator} ({moderator.id}). Reason: {reason_text[:200]}",
    )


# ==========================================================
#                      UI (VIEW/MODAL)
# ==========================================================

class DecisionReasonModal(discord.ui.Modal):
    def __init__(self, decision: str):
        self.decision = decision  # "accept" | "reject"
        title = "Комментарий / причина принятия" if decision == "accept" else "Причина отклонения"
        super().__init__(title=title)

        self.reason = discord.ui.TextInput(
            label="Текст",
            placeholder="Например: всё ок / не хватает часов / нет прайма и т.д.",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=700,
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message("Ошибка: не удалось определить канал.", ephemeral=True)

        moderator = interaction.user
        if not isinstance(moderator, discord.Member) or not is_staff(moderator):
            return await interaction.response.send_message("Недостаточно прав.", ephemeral=True)

        channel = interaction.channel
        lock = _get_channel_lock(channel.id)

        # если кто-то уже обрабатывает — не даём повторно
        if lock.locked():
            return await interaction.response.send_message(
                "Тикет уже обрабатывается другим модератором.",
                ephemeral=True,
            )

        async with lock:
            opener = await get_opener_user(channel)

            await interaction.response.send_message(
                "**Причина принята.** *Уведомляю пользователя, обновляю роли (если принят) и удаляю тикет…*",
                ephemeral=True,
            )

            # DM
            dm_ok = True
            extra_dm_ok = True
            if opener is None:
                dm_ok = False
                extra_dm_ok = False
            else:
                if self.decision == "reject":
                    dm_text = (
                        f"**Приветствую {opener.mention} ! Сожалеем, но ваша заявка в клан SH была отклонена модератором.**\n"
                        f"**Причина:** *{self.reason.value}*\n\n"
                        f"**Если хотите, то обязательно подавайте заявку повторно, мы вас обязательно ждем!**\n"
                        f"** permament link - {INVITE_LINK} **"
                    )
                else:
                    dm_text = (
                        f"**Приветствую {opener.mention} ! Отличные новости — ваша заявка в клан SH была одобрена модератором.**\n"
                        f"**Комментарий:** *{self.reason.value}*\n\n"
                        f"**Заходите на сервер и напишите модератору для дальнейших действий!**\n"
                        f"** permament link - {INVITE_LINK} **"
                    )

                try:
                    await opener.send(dm_text, allowed_mentions=discord.AllowedMentions(users=True))
                except (discord.Forbidden, discord.HTTPException):
                    dm_ok = False

                # Дополнительное сообщение при принятии
                if self.decision == "accept":
                    try:
                        await opener.send(ACCEPT_EXTRA_DM, allowed_mentions=discord.AllowedMentions.none())
                    except (discord.Forbidden, discord.HTTPException):
                        extra_dm_ok = False

            # Роли — только при принятии
            roles_status = "SKIP"
            if self.decision == "accept" and opener is not None:
                ok, code = await apply_accept_roles(
                    interaction.guild,
                    opener.id,
                    add_role_id=ACCEPT_ADD_ROLE_ID,
                    remove_role_id=ACCEPT_REMOVE_ROLE_ID,
                )
                roles_status = "OK" if ok else f"FAIL:{code}"

            # Убираем сообщение с кнопками (на случай если удаление канала не получится)
            await disable_or_delete_prompt_message(channel)

            player_text = f"{opener} ({opener.id})" if opener else "не найден"
            dm_status = "OK" if dm_ok else "FAIL"
            extra_dm_status = "OK" if extra_dm_ok else "FAIL"

            # Логи по шаблону (до удаления канала)
            await send_application_log(
                interaction.guild,
                decision=self.decision,
                opener=opener,
                moderator=moderator,
                reason_text=self.reason.value,
                dm_sent=dm_ok,
            )

            # чистим БД + in-memory кэш
            db_delete_ticket(channel.id)
            db_delete_prompt(channel.id)
            _last_prompt_time.pop(channel.id, None)

            # Сообщение модератору (ephemeral) перед удалением канала
            decision_ru = "принято ✅" if self.decision == "accept" else "отклонено ❌"
            summary = (
                f"Готово: **{decision_ru}**\n"
                f"Игрок: **{player_text}**\n"
                f"DM: **{dm_status}**"
            )
            if self.decision == "accept":
                summary += f"\nДоп. DM: **{extra_dm_status}**\nРоли: **{roles_status}**"

            try:
                await interaction.followup.send(summary, ephemeral=True)
            except discord.HTTPException:
                pass

            # Удаляем тикет-канал
            try:
                await channel.delete(
                    reason=(
                        f"[SH] decision={self.decision} by {moderator} ({moderator.id}). "
                        f"Player: {player_text}. Text: {self.reason.value[:200]}"
                    )
                )
            except (discord.Forbidden, discord.HTTPException) as e:
                # Если удалить не получилось — как запасной план переносим в архив и закрываем права
                try:
                    await archive_and_lock_channel(channel, opener, moderator, self.reason.value)
                except Exception:
                    pass
                await log_event(
                    interaction.guild,
                    f"[SH] WARNING: failed to delete channel={channel.id}. error={type(e).__name__}: {e}"
                )

            _channel_locks.pop(channel.id, None)



class TicketDecisionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Принять с комментарием",
        style=discord.ButtonStyle.success,
        custom_id="sh_accept_with_reason",
    )
    async def accept_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        moderator = interaction.user
        if not isinstance(moderator, discord.Member) or not is_staff(moderator):
            return await interaction.response.send_message("Недостаточно прав.", ephemeral=True)
        await interaction.response.send_modal(DecisionReasonModal("accept"))

    @discord.ui.button(
        label="Отклонить с причиной",
        style=discord.ButtonStyle.danger,
        custom_id="sh_reject_with_reason",
    )
    async def reject_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        moderator = interaction.user
        if not isinstance(moderator, discord.Member) or not is_staff(moderator):
            return await interaction.response.send_message("Недостаточно прав.", ephemeral=True)
        await interaction.response.send_modal(DecisionReasonModal("reject"))


# ==========================================================
#                        EVENTS
# ==========================================================

@client.event
async def on_ready():
    db_init()
    print(f"Logged in as {client.user} (ID: {client.user.id})")

    # persistent views (работают после рестарта)
    client.add_view(TicketDecisionView())
    client.add_view(PrivateSetupView())

    # сообщение с кнопкой в приватке (если бот там есть и имеет доступ)
    await ensure_private_setup_message()


@client.event
async def on_guild_channel_create(channel):
    if isinstance(channel, discord.TextChannel) and channel.category_id == TICKETS_CATEGORY_ID:
        await asyncio.sleep(1)
        try:
            msg = await channel.send(WELCOME_MESSAGE)
            # Автозакреп (нужны права Manage Messages)
            try:
                await msg.pin(reason="[SH] Auto-pin application template")
            except (discord.Forbidden, discord.HTTPException):
                pass
        except discord.HTTPException:
            pass


@client.event
async def on_message(message: discord.Message):
    # игнор своих сообщений
    if message.author and client.user and message.author.id == client.user.id:
        return

    if not message.guild or not isinstance(message.channel, discord.TextChannel):
        return

    # только тикет-каналы
    if message.channel.category_id != TICKETS_CATEGORY_ID:
        return

    # 1) сохраняем opener: первый non-bot пользователь, который НЕ staff
    if isinstance(message.author, discord.Member) and not message.author.bot:
        if not is_staff(message.author):
            if db_get_opener(message.channel.id) is None:
                db_set_opener(message.channel.id, message.author.id)

    # 2) триггер Ticket Tool
    if not message_contains_trigger(message):
        return

    # защита от подделки: игрок не должен запускать кнопки обычным сообщением
    # (разрешаем ботов и вебхуки)
    if not message.author.bot and message.webhook_id is None:
        return

    # анти-спам
    now = time.time()
    last = _last_prompt_time.get(message.channel.id, 0.0)
    if now - last < PROMPT_COOLDOWN_SECONDS:
        return
    _last_prompt_time[message.channel.id] = now

    # если opener не успели записать — попробуем фоллбеком
    if db_get_opener(message.channel.id) is None:
        opener = await resolve_ticket_opener_fallback(message.channel)
        if opener:
            db_set_opener(message.channel.id, opener.id)

    staff_ping = build_staff_ping(message.guild)
    spoiler_pings = f"||{staff_ping}||" if staff_ping else ""

    # Отступ как просили: текст -> пустая строка -> ||пинги||
    prompt_text = (
        "**Если вы хотите закрыть заявку с причиной/комментарием, нажмите на нужную кнопку ниже. "
        "Пользователь получит сообщение в личные сообщения!**\n\n"
        f"{spoiler_pings}"
    )

    try:
        sent = await message.channel.send(
            prompt_text,
            view=TicketDecisionView(),
            allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
        )
        db_set_prompt(message.channel.id, sent.id)
    except discord.HTTPException:
        pass


client.run(TOKEN)

