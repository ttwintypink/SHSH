# main.py
"""Bot entrypoint.

This bot is deployed on Linux hostings where:
- stdout/stderr may be buffered (logs appear late),
- the process can look "stuck" at "logging in using static token".

To make deployments predictable we:
- enable line-buffered stdout/stderr,
- enable discord.py logging,
- optionally force IPv4 DNS resolution (DISCORD_FORCE_IPV4=1),
- start the client as a task and watch for READY with a timeout.

Environment options:
  DISCORD_READY_TIMEOUT: seconds to wait for on_ready (default: 180)
  DISCORD_FORCE_IPV4: 1 to force IPv4 DNS resolution (default: 0)
  DISCORD_CONNECT_RETRIES: number of reconnect attempts on timeout (default: 0 = infinite)
  DISCORD_CONNECT_BACKOFF_MAX: max seconds for exponential backoff (default: 60)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Optional

import discord

from config import TOKEN
from app import client

# Important: registering handlers
import events  # noqa: F401
import slash_commands  # noqa: F401


def _enable_line_buffered_io() -> None:
    """Force logs to appear immediately in most hosting panels."""
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        os.environ.setdefault("PYTHONUNBUFFERED", "1")


def _maybe_force_ipv4() -> None:
    if str(os.getenv("DISCORD_FORCE_IPV4", "0")).lower() in {"1", "true", "yes", "on"}:
        try:
            import socket

            _orig_getaddrinfo = socket.getaddrinfo

            def _ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
                return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

            socket.getaddrinfo = _ipv4_getaddrinfo  # type: ignore[assignment]
            logging.info("🔧 DISCORD_FORCE_IPV4=1 (forcing IPv4)")
        except Exception as e:
            logging.warning("DISCORD_FORCE_IPV4 failed: %s", e)


async def _run() -> None:
    _enable_line_buffered_io()

    # Enable discord.py logs (gateway/connect problems become visible)
    try:
        discord.utils.setup_logging(level=logging.INFO)
    except Exception:
        logging.basicConfig(level=logging.INFO)

    _maybe_force_ipv4()

    timeout_s = float(os.getenv("DISCORD_READY_TIMEOUT", "180") or "180")
    retries_env = int(os.getenv("DISCORD_CONNECT_RETRIES", "0") or "0")
    backoff_max = float(os.getenv("DISCORD_CONNECT_BACKOFF_MAX", "60") or "60")

    async def _diagnose_gateway() -> None:
        """Best-effort network diagnostics for hosting environments."""
        try:
            import socket

            def _resolve(name: str) -> None:
                try:
                    infos = socket.getaddrinfo(name, 443, 0, socket.SOCK_STREAM)
                    addrs = sorted({i[4][0] for i in infos})
                    logging.info("[NET] DNS %s -> %s", name, ", ".join(addrs) if addrs else "<none>")
                except Exception as e:
                    logging.error("[NET] DNS %s failed: %s", name, e)

            _resolve("gateway.discord.gg")
            _resolve("discord.com")

            # TCP probe
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection("gateway.discord.gg", 443), timeout=8
                )
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
                logging.info("[NET] TCP gateway.discord.gg:443 OK")
            except Exception as e:
                logging.error("[NET] TCP gateway.discord.gg:443 failed: %s", e)
        except Exception as e:
            logging.warning("[NET] diagnostics skipped: %s", e)

        # HTTP probe (uses aiohttp that ships with discord.py)
        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                async with session.get("https://discord.com/api/v10/gateway", timeout=10) as resp:
                    logging.info("[NET] GET /gateway -> HTTP %s", resp.status)
        except Exception as e:
            logging.error("[NET] GET https://discord.com/api/v10/gateway failed: %s", e)

    attempt = 0
    backoff = 2.0

    while True:
        attempt += 1
        logging.info("[BOOT] Starting Discord client (attempt %s)...", attempt)
        await _diagnose_gateway()

        start_task = asyncio.create_task(client.start(TOKEN))
        ready_task = asyncio.create_task(client.wait_until_ready())

        try:
            done, _pending = await asyncio.wait(
                {start_task, ready_task},
                timeout=timeout_s,
                return_when=asyncio.FIRST_COMPLETED,
            )

            if not done:
                raise asyncio.TimeoutError()

            # If start_task finished first, it either crashed or exited.
            if start_task in done:
                exc: Optional[BaseException] = start_task.exception()
                if exc:
                    raise exc
                raise RuntimeError("Discord client stopped before READY")

            logging.info("✅ Discord READY")

            # Stop the watchdog task (READY is a one-time event)
            if not ready_task.done():
                ready_task.cancel()

            # Keep running until the bot is stopped
            await start_task
            return

        except discord.LoginFailure:
            logging.exception("❌ Токен бота неверный или отозван (LoginFailure).")
            raise
        except discord.PrivilegedIntentsRequired:
            logging.exception(
                "❌ Включите Privileged Gateway Intents в Discord Developer Portal "
                "(обычно Message Content Intent).",
            )
            raise
        except asyncio.TimeoutError:
            logging.error(
                "❌ Не дождались READY за %.0f сек. Это почти всегда сеть/вебсокет до Discord Gateway на хостинге. ",
                timeout_s,
            )
        except Exception:
            logging.exception("❌ Ошибка запуска Discord клиента (см. выше).")

        # Cleanup and retry
        try:
            await client.close()
        except Exception:
            pass
        try:
            start_task.cancel()
        except Exception:
            pass

        if retries_env > 0 and attempt >= retries_env:
            raise RuntimeError("Exceeded DISCORD_CONNECT_RETRIES")

        sleep_s = min(backoff, backoff_max)
        logging.info("[BOOT] Retry in %.0f sec...", sleep_s)
        await asyncio.sleep(sleep_s)
        backoff = min(backoff * 2.0, backoff_max)


if __name__ == "__main__":
    asyncio.run(_run())
