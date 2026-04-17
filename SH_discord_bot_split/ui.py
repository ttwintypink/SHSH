# ui.py
import asyncio
import discord

from app import _get_channel_lock, _last_prompt_time, _channel_locks
from config import (
    INVITE_LINK,  # остаётся для отказа (если хотите убрать — скажи)
    ACCEPT_EXTRA_DM,
    ACCEPT_ADD_ROLE_ID,
    ACCEPT_REMOVE_ROLE_ID,
)
from db import db_delete_ticket, db_delete_prompt
from helpers import is_staff
from logs import log_event, send_application_log
from privatka import create_one_time_private_invite
from tickets import (
    get_opener_user,
    apply_accept_roles,
    disable_or_delete_prompt_message,
    archive_and_lock_channel,
)


def explain_roles_code(code: str) -> str:
    """Человекочитаемое пояснение, почему роли могли не обновиться."""
    base = code.split(":", 1)[0]
    mapping = {
        "member_not_found": "участник не найден в гильдии (вышел/кикнут/нет доступа к участникам)",
        "roles_not_found": "роль(и) не найдены по ID в config.py",
        "bot_member_not_found": "не удалось получить Member бота в гильдии",
        "bot_missing_manage_roles": "у бота нет права **Manage Roles**",
        "target_hierarchy_too_high": "у игрока есть роль выше/равная роли бота (иерархия)",
        "add_role_hierarchy": "роль для выдачи выше/равна роли бота (подними роль бота выше)",
        "remove_role_hierarchy": "роль для снятия выше/равна роли бота (подними роль бота выше)",
        "add_role_managed": "роль для выдачи managed (интеграция) — её нельзя выдать ботом",
        "remove_role_managed": "роль для снятия managed (интеграция) — её нельзя снять ботом",
        "forbidden_manage_roles_or_hierarchy": "Discord запретил изменение ролей (Manage Roles / иерархия ролей)",
        "http_exception": "ошибка Discord API при изменении ролей",
    }
    return mapping.get(base, base)

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

            # ------------------------------------------------------
            # Инвайт в приватку (только при принятии)
            # ------------------------------------------------------
            invite_url: str | None = None
            invite_ok = False
            if self.decision == "accept" and opener is not None:
                invite = await create_one_time_private_invite(opener=opener, moderator=moderator)
                if invite:
                    invite_url = invite.url
                    invite_ok = True

            # ------------------------------------------------------
            # DM пользователю
            # ------------------------------------------------------
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
                        f"**permanent link:** {INVITE_LINK}"
                    )
                else:
                    invite_line = (
                        f"**Персональная ссылка в приватку (1 раз, действует 24 часа):** {invite_url}"
                        if invite_url
                        else "**Ссылка в приватку:** *(не удалось создать автоматически — напишите модератору)*"
                    )
                    dm_text = (
                        f"**Приветствую {opener.mention} ! Отличные новости — ваша заявка в клан SH была одобрена модератором.**\n"
                        f"**Комментарий:** *{self.reason.value}*\n\n"
                        f"{invite_line}"
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

            # ------------------------------------------------------
            # Роли — только при принятии
            # ------------------------------------------------------
            roles_status = "SKIP"
            roles_human: str | None = None
            if self.decision == "accept" and opener is not None:
                ok, code = await apply_accept_roles(
                    interaction.guild,
                    opener.id,
                    add_role_id=ACCEPT_ADD_ROLE_ID,
                    remove_role_id=ACCEPT_REMOVE_ROLE_ID,
                )
                if ok:
                    roles_status = "OK"
                else:
                    roles_status = f"FAIL:{code}"
                    roles_human = explain_roles_code(code)
                    # Пишем в лог-канал, чтобы было видно причину
                    await log_event(
                        interaction.guild,
                        f"[SH] roles update failed for user={opener.id} in channel={channel.id}: {code}",
                    )

            # Убираем сообщение с кнопками (на случай если удаление канала не получится)
            await disable_or_delete_prompt_message(channel)

            player_text = f"{opener} ({opener.id})" if opener else "не найден"
            dm_status = "OK" if dm_ok else "FAIL"
            extra_dm_status = "OK" if extra_dm_ok else "FAIL"
            invite_status = "OK" if invite_ok else "FAIL"

            roles_line = f"**{roles_status}**"
            if roles_human:
                roles_line += f" ({roles_human})"

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
                summary += (
                    f"\nИнвайт в приватку: **{invite_status}**"
                    f"\nДоп. DM: **{extra_dm_status}**\nРоли: {roles_line}"
                )
                if invite_url:
                    summary += f"\nСсылка (для тебя): {invite_url}"

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
