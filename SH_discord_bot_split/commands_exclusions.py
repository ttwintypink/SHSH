# commands_exclusions.py
# Slash-команды для управления списком исключения "opener".

from __future__ import annotations

import re
import discord
from discord import app_commands

from app import tree, client
from config import IGNORE_ADD_ADMIN_ID
from db import (
    db_add_ignored_user,
    db_is_ignored_user,
    db_remove_ignored_user,
    db_list_ignored_users,
)
from helpers import is_staff

_ID_RE = re.compile(r"<@!?([0-9]{15,25})>|\b([0-9]{15,25})\b")


def _parse_user_id(raw: str) -> int | None:
    if not raw:
        return None
    m = _ID_RE.search(raw.strip())
    if not m:
        return None
    s = m.group(1) or m.group(2)
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def _can_manage(interaction: discord.Interaction) -> bool:
    user = interaction.user
    if isinstance(user, discord.Member):
        if user.id == IGNORE_ADD_ADMIN_ID:
            return True
        if user.guild_permissions.administrator:
            return True
        if is_staff(user):
            return True
    return False


async def _display_name(guild: discord.Guild | None, user_id: int) -> str:
    """Пытаемся показать display_name в контексте сервера; если нет — username."""
    if guild:
        m = guild.get_member(user_id)
        if m:
            return m.display_name
        try:
            m = await guild.fetch_member(user_id)
            return m.display_name
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
    try:
        u = await client.fetch_user(user_id)
        return u.name
    except discord.HTTPException:
        return "unknown"


class _ConfirmAddView(discord.ui.View):
    def __init__(self, *, requester_id: int, target_id: int):
        super().__init__(timeout=60)
        self.requester_id = requester_id
        self.target_id = target_id
        self._done = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Эта кнопка не для вас.",
                ephemeral=True,
            )
            return False
        return True

    async def _finish(self, interaction: discord.Interaction, text: str) -> None:
        self._done = True
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        try:
            await interaction.response.edit_message(content=text, view=self)
        except discord.HTTPException:
            pass
        self.stop()

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.green)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        db_add_ignored_user(self.target_id, interaction.user.id)
        name = await _display_name(interaction.guild, self.target_id)
        await self._finish(interaction, f"✅ Добавлено в списки исключения: {self.target_id} - {name}")

    @discord.ui.button(label="No", style=discord.ButtonStyle.red)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await self._finish(interaction, "Отменено.")


@tree.command(name="add", description="Добавить пользователя в списки исключения")
@app_commands.describe(user_id="ID пользователя или @упоминание")
async def add_cmd(interaction: discord.Interaction, user_id: str):
    if interaction.guild is None:
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return
    if not _can_manage(interaction):
        await interaction.response.send_message("Недостаточно прав.", ephemeral=True)
        return

    uid = _parse_user_id(user_id)
    if uid is None:
        await interaction.response.send_message("Не смог распознать ID. Пример: `/add 123456789012345678`", ephemeral=True)
        return

    already = db_is_ignored_user(uid)
    db_add_ignored_user(uid, interaction.user.id)
    name = await _display_name(interaction.guild, uid)

    if already:
        await interaction.response.send_message(f"ℹ️ Уже был в исключениях: {uid} - {name}", ephemeral=True)
    else:
        await interaction.response.send_message(f"✅ Добавлено в исключения: {uid} - {name}", ephemeral=True)


@tree.command(name="del", description="Убрать пользователя из списков исключения")
@app_commands.describe(user_id="ID пользователя или @упоминание")
async def del_cmd(interaction: discord.Interaction, user_id: str):
    if interaction.guild is None:
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return
    if not _can_manage(interaction):
        await interaction.response.send_message("Недостаточно прав.", ephemeral=True)
        return

    uid = _parse_user_id(user_id)
    if uid is None:
        await interaction.response.send_message("Не смог распознать ID. Пример: `/del 123456789012345678`", ephemeral=True)
        return

    if not db_is_ignored_user(uid):
        # Особое поведение: если именно 1166060811672883210 делает /del, предлагаем добавить.
        if interaction.user.id == IGNORE_ADD_ADMIN_ID:
            name = await _display_name(interaction.guild, uid)
            text = (
                "Данный пользователь не находится в списках исключения. Добавить?\n"
                f"**{uid} - {name}**"
            )
            await interaction.response.send_message(
                text,
                view=_ConfirmAddView(requester_id=interaction.user.id, target_id=uid),
                ephemeral=True,
            )
            return

        await interaction.response.send_message("Данный пользователь не находится в списках исключения.", ephemeral=True)
        return

    removed = db_remove_ignored_user(uid)
    name = await _display_name(interaction.guild, uid)
    if removed:
        await interaction.response.send_message(f"✅ Удалено из исключений: {uid} - {name}", ephemeral=True)
    else:
        await interaction.response.send_message("Не удалось удалить (ошибка БД).", ephemeral=True)


@tree.command(name="menu", description="Показать список исключений")
async def menu_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return
    if not _can_manage(interaction):
        await interaction.response.send_message("Недостаточно прав.", ephemeral=True)
        return

    ids = db_list_ignored_users()
    if not ids:
        await interaction.response.send_message("Списки исключения пусты.", ephemeral=True)
        return

    lines: list[str] = []
    for uid in ids:
        name = await _display_name(interaction.guild, uid)
        lines.append(f"{uid} - {name}")

    # режем на чанки под лимит Discord
    chunks: list[str] = []
    cur = ""
    for line in lines:
        add = line + "\n"
        if len(cur) + len(add) > 1800:
            chunks.append(cur.rstrip())
            cur = add
        else:
            cur += add
    if cur.strip():
        chunks.append(cur.rstrip())

    header = f"**Список исключений ({len(ids)}):**\n"
    await interaction.response.send_message(header + chunks[0], ephemeral=True)
    for chunk in chunks[1:]:
        await interaction.followup.send(chunk, ephemeral=True)
