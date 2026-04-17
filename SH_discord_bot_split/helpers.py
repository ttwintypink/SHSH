# helpers.py
import re
import discord

from config import STAFF_ROLE_IDS, STAFF_PING_ROLE_IDS, TRIGGER_PHRASE


# ==========================================================
#                    HELPER FUNCTIONS
# ==========================================================


def is_staff(member: discord.Member) -> bool:
    # Жёстко по ролям + админ
    if member.guild_permissions.administrator:
        return True
    return any(r.id in STAFF_ROLE_IDS for r in member.roles)


def _normalize_text(text: str) -> str:
    text = (text or "").lower().strip()
    # Ticket Tool и другие боты могут использовать "ё"/"е" по-разному.
    # Для устойчивого триггера приводим к единому виду.
    text = text.replace("ё", "е")
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

    joined_norm = _normalize_text(" ".join(parts))
    trigger_norm = _normalize_text(TRIGGER_PHRASE)
    return trigger_norm in joined_norm


def build_staff_ping(guild: discord.Guild) -> str:
    mentions = []
    for rid in STAFF_PING_ROLE_IDS:
        role = guild.get_role(rid)
        mentions.append(role.mention if role else f"<@&{rid}>")
    return " ".join(mentions).strip()
