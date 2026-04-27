from __future__ import annotations

import asyncio
import time
import discord

# Защита от долбёжки Discord API по /guilds/{guild}/members/{user}
# 1) сначала кэш guild.get_member
# 2) отрицательный/положительный TTL-кэш
# 3) лок на конкретного пользователя, чтобы параллельные команды не делали одинаковые GET
# 4) мягкая пауза между REST-fetch на одну гильдию

POSITIVE_TTL = 20 * 60       # 20 минут храним найденного участника
NEGATIVE_TTL = 5 * 60        # 5 минут храним, что участника нет
FETCH_GAP_SECONDS = 0.35     # не чаще ~3 fetch_member/сек на гильдию

_member_cache: dict[tuple[int, int], tuple[float, discord.Member | None]] = {}
_member_locks: dict[tuple[int, int], asyncio.Lock] = {}
_guild_fetch_locks: dict[int, asyncio.Lock] = {}
_last_guild_fetch_at: dict[int, float] = {}
_chunk_locks: dict[int, asyncio.Lock] = {}
_last_chunk_at: dict[int, float] = {}


def _now() -> float:
    return time.monotonic()


def forget_member(guild_id: int, user_id: int) -> None:
    _member_cache.pop((guild_id, user_id), None)


def get_cached_member(guild: discord.Guild, user_id: int) -> discord.Member | None:
    member = guild.get_member(user_id)
    if member is not None:
        _member_cache[(guild.id, user_id)] = (_now() + POSITIVE_TTL, member)
        return member

    item = _member_cache.get((guild.id, user_id))
    if not item:
        return None

    expires_at, cached = item
    if expires_at <= _now():
        _member_cache.pop((guild.id, user_id), None)
        return None
    return cached


async def safe_fetch_member(
    guild: discord.Guild,
    user_id: int,
    *,
    allow_fetch: bool = True,
) -> discord.Member | None:
    """Безопасно получить Member без спама Discord API."""
    member = get_cached_member(guild, user_id)
    if member is not None:
        return member

    # Если в кэше лежит отрицательный результат и он не истёк — не fetch'им повторно.
    item = _member_cache.get((guild.id, user_id))
    if item and item[0] > _now() and item[1] is None:
        return None

    if not allow_fetch:
        return None

    key = (guild.id, user_id)
    lock = _member_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _member_locks[key] = lock

    async with lock:
        member = get_cached_member(guild, user_id)
        if member is not None:
            return member
        item = _member_cache.get(key)
        if item and item[0] > _now() and item[1] is None:
            return None

        guild_lock = _guild_fetch_locks.get(guild.id)
        if guild_lock is None:
            guild_lock = asyncio.Lock()
            _guild_fetch_locks[guild.id] = guild_lock

        async with guild_lock:
            elapsed = _now() - _last_guild_fetch_at.get(guild.id, 0.0)
            if elapsed < FETCH_GAP_SECONDS:
                await asyncio.sleep(FETCH_GAP_SECONDS - elapsed)
            _last_guild_fetch_at[guild.id] = _now()

            try:
                member = await guild.fetch_member(user_id)
            except discord.NotFound:
                _member_cache[key] = (_now() + NEGATIVE_TTL, None)
                return None
            except (discord.Forbidden, discord.HTTPException):
                # На ошибке не долбим повторно сразу.
                _member_cache[key] = (_now() + 60, None)
                return None

            _member_cache[key] = (_now() + POSITIVE_TTL, member)
            return member


async def warm_guild_member_cache(guild: discord.Guild, *, min_interval: int = 600) -> bool:
    """
    Пробует загрузить участников гильдии через Gateway chunk.
    Это лучше, чем делать сотни REST GET /members.
    Работает при включённом SERVER MEMBERS INTENT.
    """
    now = _now()
    if now - _last_chunk_at.get(guild.id, 0.0) < min_interval:
        return True

    lock = _chunk_locks.get(guild.id)
    if lock is None:
        lock = asyncio.Lock()
        _chunk_locks[guild.id] = lock

    async with lock:
        now = _now()
        if now - _last_chunk_at.get(guild.id, 0.0) < min_interval:
            return True
        try:
            members = await guild.chunk(cache=True)
            for member in members or []:
                _member_cache[(guild.id, member.id)] = (_now() + POSITIVE_TTL, member)
            _last_chunk_at[guild.id] = _now()
            return True
        except Exception:
            _last_chunk_at[guild.id] = _now()
            return False
