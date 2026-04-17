# tickets.py
import re
import discord

from app import client
from config import ARCHIVE_CATEGORY_ID, STAFF_PING_ROLE_IDS, IGNORED_TICKET_OPENER_IDS, IGNORED_TICKET_OPENER_ROLE_IDS
from db import (
    db_get_opener,
    db_set_opener,
    db_get_prompt,
    db_delete_prompt,
    db_is_ignored_user,
)
from helpers import is_staff


def is_ignored_ticket_opener_id(user_id: int) -> bool:
    # статический список + динамический (из БД)
    if user_id in IGNORED_TICKET_OPENER_IDS:
        return True
    return db_is_ignored_user(user_id)



def is_ignored_ticket_opener_member(member: discord.Member) -> bool:
    """True если участник не должен считаться opener (по ID или по роли)."""
    # по ID (статический список + динамический из БД)
    if is_ignored_ticket_opener_id(member.id):
        return True
    # Роли
    try:
        role_ids = {r.id for r in member.roles}
    except Exception:
        role_ids = set()
    return any(rid in IGNORED_TICKET_OPENER_ROLE_IDS for rid in role_ids)


def is_valid_ticket_opener_member(member: discord.Member) -> bool:
    """True если участник может считаться автором тикета."""
    if member.bot:
        return False
    if is_staff(member):
        return False
    if is_ignored_ticket_opener_member(member):
        return False
    return True


def _is_valid_opener_member(member: discord.Member) -> bool:
    # backward-compat (старое имя)
    return is_valid_ticket_opener_member(member)


async def resolve_ticket_opener_fallback(channel: discord.TextChannel) -> discord.abc.User | None:
    # 1) topic
    if channel.topic:
        m = re.search(r"<@!?(\d{15,25})>", channel.topic)
        if m:
            uid = int(m.group(1))
            if not is_ignored_ticket_opener_id(uid):
                member = channel.guild.get_member(uid)
                if isinstance(member, discord.Member) and _is_valid_opener_member(member):
                    return member
                try:
                    u = await client.fetch_user(uid)
                    return u
                except discord.HTTPException:
                    pass

        m = re.search(r"\b(\d{15,25})\b", channel.topic)
        if m:
            uid = int(m.group(1))
            if not is_ignored_ticket_opener_id(uid):
                member = channel.guild.get_member(uid)
                if isinstance(member, discord.Member) and _is_valid_opener_member(member):
                    return member
                try:
                    u = await client.fetch_user(uid)
                    return u
                except discord.HTTPException:
                    pass

    # 2) overwrites
    for target, ow in channel.overwrites.items():
        if isinstance(target, discord.Member) and ow.view_channel is True:
            if _is_valid_opener_member(target):
                return target

    # 3) history
    try:
        async for m in channel.history(limit=200, oldest_first=True):
            if isinstance(m.author, discord.Member) and _is_valid_opener_member(m.author):
                return m.author
            # если author не Member (например, в некоторых случаях), проверим по id
            if m.author and not getattr(m.author, "bot", False):
                if not is_ignored_ticket_opener_id(m.author.id):
                    return m.author
    except discord.HTTPException:
        pass

    return None


async def get_opener_user(channel: discord.TextChannel) -> discord.abc.User | None:
    opener_id = db_get_opener(channel.id)
    if opener_id:
        # если в БД лежит "игнорируемый" ID — пробуем определить заново
        if is_ignored_ticket_opener_id(opener_id):
            opener_id = None
        else:
            member = channel.guild.get_member(opener_id)
            if member and isinstance(member, discord.Member) and _is_valid_opener_member(member):
                return member
            try:
                return await client.fetch_user(opener_id)
            except discord.HTTPException:
                opener_id = None

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

    Возвращает (ok, code), где code — диагностический код (для логов/модераторов).
    """
    member = await ensure_guild_member(guild, opener_id)
    if not member:
        return False, "member_not_found"

    # Роли (0/None трактуем как "не использовать")
    add_role = guild.get_role(add_role_id) if add_role_id else None
    rem_role = guild.get_role(remove_role_id) if remove_role_id else None
    if add_role is None and rem_role is None:
        return False, "roles_not_found"

    # Бот в гильдии (для проверок прав/иерархии)
    bot_member: discord.Member | None = getattr(guild, "me", None)
    if bot_member is None:
        try:
            if client.user:
                bot_member = guild.get_member(client.user.id) or await guild.fetch_member(client.user.id)
        except Exception:
            bot_member = None

    if bot_member is None:
        return False, "bot_member_not_found"

    # Проверка прав
    if not bot_member.guild_permissions.manage_roles:
        return False, "bot_missing_manage_roles"

    bot_top = bot_member.top_role

    # Нельзя менять роли у участника, если его топ-роль >= топ-роли бота
    try:
        if member.top_role >= bot_top:
            return False, "target_hierarchy_too_high"
    except Exception:
        pass

    # Проверка иерархии/managed для ролей
    def _check_role(r: discord.Role | None, kind: str) -> str | None:
        if r is None:
            return None
        if getattr(r, "managed", False):
            return f"{kind}_role_managed"
        # роль должна быть строго ниже роли бота
        if r >= bot_top:
            return f"{kind}_role_hierarchy"
        return None

    err = _check_role(add_role, "add") or _check_role(rem_role, "remove")
    if err:
        return False, err

    # Применяем
    try:
        if rem_role and rem_role in member.roles:
            await member.remove_roles(rem_role, reason="[SH] Ticket accepted: remove role")
        if add_role and add_role not in member.roles:
            await member.add_roles(add_role, reason="[SH] Ticket accepted: add role")
        return True, "ok"
    except discord.Forbidden:
        return False, "forbidden_manage_roles_or_hierarchy"
    except discord.HTTPException as e:
        return False, f"http_exception:{getattr(e, 'status', '')}".rstrip(":")

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
