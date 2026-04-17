# events.py
import asyncio
import time
import re
from datetime import timedelta
import discord

from app import client, tree, _last_prompt_time
from config import (
    TICKETS_CATEGORY_ID,
    PROMPT_COOLDOWN_SECONDS,
    WELCOME_MESSAGE,
    IGNORE_ADD_ADMIN_ID,
)
from db import (
    db_init,
    db_get_opener,
    db_set_opener,
    db_set_prompt,
    db_get_prompt,
    db_delete_prompt,
)
from helpers import is_staff, message_contains_trigger, build_staff_ping
from privatka import ensure_private_setup_message, PrivateSetupView
from tickets import (
    resolve_ticket_opener_fallback,
    is_ignored_ticket_opener_id,
    is_ignored_ticket_opener_member,
    ensure_guild_member,
    is_valid_ticket_opener_member,
)
from ui import TicketDecisionView


# ==========================================================
#                        EVENTS
# ==========================================================

_ID_RE = re.compile(r"<@!?(\d{15,25})>|\b(\d{15,25})\b")


def _extract_user_ids(text: str) -> list[int]:
    ids: list[int] = []
    for m in _ID_RE.finditer(text or ""):
        g1 = m.group(1)
        g2 = m.group(2)
        raw = g1 or g2
        if raw:
            try:
                ids.append(int(raw))
            except ValueError:
                pass
    # убираем дубли, сохраняя порядок
    out = []
    seen = set()
    for x in ids:
        if x not in seen:
            out.append(x)
            seen.add(x)
    return out


def _is_simple_id_message(text: str) -> bool:
    """True если в тексте по сути только ID/упоминания (можно несколько), без лишних слов."""
    if not text:
        return False
    # убираем все ID/mentions
    cleaned = _ID_RE.sub("", text)
    # разрешаем только пробелы и разделители
    cleaned = cleaned.strip().replace(",", "").replace(";", "").replace("|", "")
    return cleaned.strip() == ""


async def _try_set_opener_from_tickettool_ping(message: discord.Message) -> None:
    """
    Ticket Tool при открытии тикета обычно первой строкой пингует автора тикета.
    Это самый надёжный источник opener_id.

    Логика:
    - смотрим упоминания/ID в сообщении от бота/вебхука
    - берём ПЕРВОГО валидного участника (не staff, не bot, не ignored)
    - записываем в БД (можем перезаписать неверно определённого opener)
    - ограничиваемся ранним окном после создания канала, чтобы случайные упоминания позже
      не меняли opener.
    """
    if not message.guild or not isinstance(message.channel, discord.TextChannel):
        return

    channel = message.channel

    # только тикеты
    if channel.category_id != TICKETS_CATEGORY_ID:
        return

    # только ранние сообщения в канале (пока тикет открывается)
    try:
        if channel.created_at and message.created_at:
            # Ticket Tool пишет пинг почти сразу; окно делаем небольшим,
            # чтобы случайные упоминания позже не перетирали opener.
            if message.created_at - channel.created_at > timedelta(minutes=5):
                return
    except Exception:
        # если что-то не так с датами — не блокируем
        pass

    # это должен быть бот/вебхук (Ticket Tool и т.п.)
    if not (getattr(message.author, "bot", False) or message.webhook_id is not None):
        return

    candidate_ids: list[int] = []

    # 1) самый точный вариант: упоминание пользователя в НАЧАЛЕ сообщения.
    # Именно так Ticket Tool обычно пингует автора тикета.
    if message.content:
        m = re.match(r"^\s*<@!?(\d{15,25})>", message.content)
        if m:
            try:
                candidate_ids.append(int(m.group(1)))
            except ValueError:
                pass

    # 2) дальше — обычные mentions (если формат сообщения отличается)
    if not candidate_ids:
        try:
            candidate_ids.extend([m.id for m in message.mentions if isinstance(m, discord.Member)])
        except Exception:
            pass

    # 3) фоллбек: вытащим ID из текста вручную (если бот пишет голыми числами)
    if not candidate_ids and message.content:
        candidate_ids.extend(_extract_user_ids(message.content))

    # убираем дубли
    seen: set[int] = set()
    uniq_ids: list[int] = []
    for uid in candidate_ids:
        if uid not in seen:
            uniq_ids.append(uid)
            seen.add(uid)

    if not uniq_ids:
        return

    for uid in uniq_ids:
        if is_ignored_ticket_opener_id(uid):
            continue

        member = await ensure_guild_member(message.guild, uid)
        if not member:
            continue

        if not is_valid_ticket_opener_member(member):
            continue

        # Записываем opener, даже если он уже был записан ранее (чинит неверные определения)
        db_set_opener(channel.id, member.id)
        return


def _build_prompt_text(guild: discord.Guild) -> str:
    staff_ping = build_staff_ping(guild)
    spoiler_pings = f"||{staff_ping}||" if staff_ping else ""
    # Отступ как просили: текст -> пустая строка -> ||пинги||
    return (
        "**Если вы хотите закрыть заявку с причиной/комментарием, нажмите на нужную кнопку ниже. "
        "Пользователь получит сообщение в личные сообщения!**\n\n"
        f"{spoiler_pings}"
    )


async def ensure_decision_prompt(channel: discord.TextChannel, *, reason: str = "") -> None:
    """Гарантированно пытаемся отправить сообщение с кнопками.

    - Не шлём дубликаты, если сообщение уже есть (и живое).
    - Делаем длиннее ретраи, т.к. сразу после создания тикета права/интеграции иногда не успевают.
    """

    # если в БД уже есть prompt — проверим что сообщение реально существует
    existing_id = db_get_prompt(channel.id)
    if existing_id:
        try:
            await channel.fetch_message(existing_id)
            return
        except discord.NotFound:
            db_delete_prompt(channel.id)
        except discord.HTTPException:
            # не смогли проверить — лучше не спамить, но дадим шанс отправке ниже
            pass

    prompt_text = _build_prompt_text(channel.guild)

    # ретраи ~ до 1 минуты
    last_err: Exception | None = None
    for delay in (0, 1, 2, 4, 8, 16, 32):
        if delay:
            await asyncio.sleep(delay)
        try:
            sent = await channel.send(
                prompt_text,
                view=TicketDecisionView(),
                allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
            )
            db_set_prompt(channel.id, sent.id)
            return
        except (discord.Forbidden, discord.HTTPException) as e:
            last_err = e
            continue

    # если совсем не получилось — хотя бы залогируем (в консоль)
    if last_err:
        print(f"[Prompt] FAILED channel={channel.id} reason={reason} error={type(last_err).__name__}: {last_err}")


@client.event
async def on_ready():
    db_init()
    print(f"Logged in as {client.user} (ID: {client.user.id})")

    # persistent views (работают после рестарта)
    client.add_view(TicketDecisionView())
    client.add_view(PrivateSetupView())

    # сообщение с кнопкой в приватке (если бот там есть и имеет доступ)
    await ensure_private_setup_message()

    # ------------------------------------------------------
    # Slash-команды: делаем "по красоте" — регистрируем в КАЖДОЙ гильдии как guild commands.
    # Почему так:
    #   - Global commands могут появляться с задержкой (иногда минуты/часы).
    #   - Guild commands появляются почти сразу.
    # Ключевой момент: наши /add /del /menu объявлены как GLOBAL, поэтому перед sync
    # копируем их в гильдию через copy_global_to().
    # Важно: если бот был приглашён БЕЗ scope "applications.commands", команды НЕ появятся.
    # ------------------------------------------------------
    global_cmds = tree.get_commands()
    print(f"[SlashSync] global_commands_loaded={len(global_cmds)}")

    total_synced = 0
    for g in list(client.guilds):
        try:
            tree.copy_global_to(guild=discord.Object(id=g.id))
            synced = await tree.sync(guild=discord.Object(id=g.id))
            total_synced += len(synced)
            print(f"[SlashSync] guild={g.name} ({g.id}) synced={len(synced)}")
        except discord.HTTPException as e:
            print(f"[SlashSync] guild={g.name} ({g.id}) FAILED: {e}")
        except Exception as e:
            print(f"[SlashSync] guild={g.name} ({g.id}) FAILED: {e}")
    print(f"[SlashSync] total_synced={total_synced}")


@client.event
async def on_guild_channel_create(channel):
    if isinstance(channel, discord.TextChannel) and channel.category_id == TICKETS_CATEGORY_ID:
        await asyncio.sleep(2)
        # 1) шаблон/приветствие
        try:
            msg = await channel.send(WELCOME_MESSAGE)
            # Автозакреп (нужны права Manage Messages)
            try:
                await msg.pin(reason="[SH] Auto-pin application template")
            except (discord.Forbidden, discord.HTTPException):
                pass
        except discord.HTTPException:
            pass

        # ВАЖНО: панель модератора (Принять/Отклонить) НЕ отправляем при создании тикета.
        # По требованиям она должна появляться только после нажатия пользователем
        # "🔒 Закрыть заявку" и появления следующего сообщения от Ticket Tool
        # ("Вы серьезно хотите закрыть данный тикет?").


@client.event
async def on_message(message: discord.Message):
    # игнор своих сообщений
    if message.author and client.user and message.author.id == client.user.id:
        return

    # ------------------------------------------------------
    # Админская ручная синхронизация slash-команд (на случай, если хостинг/рестарт и т.п.)
    # Работает и в ЛС, и в любом канале.
    # ------------------------------------------------------
    if message.author and message.author.id == IGNORE_ADD_ADMIN_ID and message.content:
        cmd = message.content.strip().lower()
        if cmd in {"!sync", "!resync"}:
            results = []
            for g in list(client.guilds):
                try:
                    tree.copy_global_to(guild=discord.Object(id=g.id))
                    synced = await tree.sync(guild=discord.Object(id=g.id))
                    results.append(f"{g.name}: {len(synced)}")
                except Exception as e:
                    results.append(f"{g.name}: FAIL ({type(e).__name__})")
            try:
                await message.channel.send(
                    "🔁 Sync done. " + " | ".join(results),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                pass
            return


    # дальше нас интересуют только сообщения на сервере, в текстовых каналах
    if not message.guild or not isinstance(message.channel, discord.TextChannel):
        return

    # только тикет-каналы
    if message.channel.category_id != TICKETS_CATEGORY_ID:
        return

    # 0) самый надёжный способ определить автора тикета:
    # Ticket Tool при открытии канала первой строкой пингует пользователя.
    # Фиксируем именно этого пользователя как opener.
    try:
        await _try_set_opener_from_tickettool_ping(message)
    except Exception as e:
        # не даём упасть обработчику сообщений из-за сторонних особенностей
        print(f"[SH] WARNING: opener-detect failed channel={message.channel.id}. {type(e).__name__}: {e}")

    # 1) сохраняем opener: первый non-bot пользователь, который НЕ staff и НЕ в игноре
    if isinstance(message.author, discord.Member) and not message.author.bot:
        if (not is_staff(message.author)) and (not is_ignored_ticket_opener_member(message.author)):
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
            if isinstance(opener, discord.Member):
                if not is_ignored_ticket_opener_member(opener):
                    db_set_opener(message.channel.id, opener.id)
            else:
                # Если по какой-то причине получили не Member, то проверяем только по ID
                if not is_ignored_ticket_opener_id(opener.id):
                    db_set_opener(message.channel.id, opener.id)

    # 3) На всякий случай ещё раз гарантируем наличие панели модератора.
    # (например, если channel_create не сработал/не успел или сообщение было удалено)
    await ensure_decision_prompt(message.channel, reason="ticket_tool_trigger")

