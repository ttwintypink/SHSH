# app.py
import asyncio
import discord
from discord import app_commands

from config import TOKEN  # noqa: F401 (важно: валидирует токен из ENV при импорте)
# выше нужен только для валидации: логика совпадает с монолитной версией

# ==========================================================
#                      INTENTS / CLIENT
# ==========================================================

intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.message_content = True  # включить в Dev Portal -> Privileged Gateway Intents
intents.members = True

client = discord.Client(intents=intents)

# Slash commands (Application Commands)
tree = app_commands.CommandTree(client)

_last_prompt_time: dict[int, float] = {}
_channel_locks: dict[int, asyncio.Lock] = {}


def _get_channel_lock(channel_id: int) -> asyncio.Lock:
    lock = _channel_locks.get(channel_id)
    if lock is None:
        lock = asyncio.Lock()
        _channel_locks[channel_id] = lock
    return lock
