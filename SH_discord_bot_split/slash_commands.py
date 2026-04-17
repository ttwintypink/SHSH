from __future__ import annotations

import discord
from discord import app_commands

from app import tree
from config import (
    PUBLIC_GUILD_ID,
    PRIVATE_GUILD_ID,
    PUBLIC_ROLE_SH_ID,
    PUBLIC_ROLE_FUN_SH_ID,
    PRIVATE_ROLE_SH_ID,
)


def _is_admin(interaction: discord.Interaction) -> bool:
    user = interaction.user
    return isinstance(user, discord.Member) and user.guild_permissions.administrator


async def _sync_member_roles(client: discord.Client, member_id: int) -> tuple[bool, str]:
    public_guild = client.get_guild(PUBLIC_GUILD_ID)
    private_guild = client.get_guild(PRIVATE_GUILD_ID)
    if public_guild is None:
        try:
            public_guild = await client.fetch_guild(PUBLIC_GUILD_ID)
        except Exception:
            return False, f"Публичный сервер {PUBLIC_GUILD_ID} не найден."
    if private_guild is None:
        try:
            private_guild = await client.fetch_guild(PRIVATE_GUILD_ID)
        except Exception:
            return False, f"Приватный сервер {PRIVATE_GUILD_ID} не найден."

    try:
        public_member = public_guild.get_member(member_id) or await public_guild.fetch_member(member_id)
    except Exception:
        return False, f"Пользователь {member_id} не найден в паблике."

    try:
        private_member = private_guild.get_member(member_id) or await private_guild.fetch_member(member_id)
    except Exception:
        private_member = None

    public_sh = public_guild.get_role(PUBLIC_ROLE_SH_ID)
    public_fun_sh = public_guild.get_role(PUBLIC_ROLE_FUN_SH_ID)
    private_sh = private_guild.get_role(PRIVATE_ROLE_SH_ID) if private_guild else None
    if public_sh is None or public_fun_sh is None or private_sh is None:
        return False, "Одна из ролей синхронизации не найдена."

    target_role = public_sh if (private_member and private_sh in private_member.roles) else public_fun_sh
    remove_role = public_fun_sh if target_role.id == public_sh.id else public_sh

    changed = []
    if remove_role in public_member.roles:
        await public_member.remove_roles(remove_role, reason='[SH] Role sync remove old role')
        changed.append(f'-{remove_role.name}')
    if target_role not in public_member.roles:
        await public_member.add_roles(target_role, reason='[SH] Role sync apply target role')
        changed.append(f'+{target_role.name}')

    if not changed:
        return True, f'{public_member} уже имеет правильную роль: {target_role.name}'
    return True, f'{public_member}: ' + ', '.join(changed)


@tree.command(name='sync', description='Синхронизировать SH/FUN SH для одного участника')
@app_commands.describe(user='Пользователь для синхронизации')
async def sync_cmd(interaction: discord.Interaction, user: discord.Member):
    if not _is_admin(interaction):
        await interaction.response.send_message('Нужны права администратора.', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        ok, text = await _sync_member_roles(interaction.client, user.id)
        await interaction.followup.send(('✅ ' if ok else '❌ ') + text, ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send('❌ Боту не хватает прав на управление ролями.', ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f'❌ Ошибка синхронизации: {type(e).__name__}: {e}', ephemeral=True)


@tree.command(name='syncall', description='Синхронизировать SH/FUN SH для всех участников паблика')
async def syncall_cmd(interaction: discord.Interaction):
    if not _is_admin(interaction):
        await interaction.response.send_message('Нужны права администратора.', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    public_guild = interaction.client.get_guild(PUBLIC_GUILD_ID)
    if public_guild is None:
        await interaction.followup.send(f'❌ Публичный сервер {PUBLIC_GUILD_ID} не найден.', ephemeral=True)
        return

    processed = 0
    changed = 0
    failed = 0
    examples: list[str] = []
    for member in public_guild.members:
        if member.bot:
            continue
        processed += 1
        try:
            ok, text = await _sync_member_roles(interaction.client, member.id)
            if ok and 'уже имеет правильную роль' not in text:
                changed += 1
                if len(examples) < 10:
                    examples.append(text)
            elif not ok:
                failed += 1
                if len(examples) < 10:
                    examples.append(text)
        except Exception as e:
            failed += 1
            if len(examples) < 10:
                examples.append(f'{member}: {type(e).__name__}: {e}')

    summary = f'Готово. Проверено: {processed}, изменено: {changed}, ошибок: {failed}.'
    if examples:
        summary += '

' + '
'.join(examples)
    await interaction.followup.send(summary[:1900], ephemeral=True)
