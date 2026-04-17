"""Bothost/Panel entrypoint.

Some hosts expect a top-level `main.py` in the repository root.
This wrapper simply delegates to the real bot entrypoint:
    SH_discord_bot_split/main.py

It keeps `__name__ == '__main__'` semantics and preserves stdout/stderr.
"""

from __future__ import annotations

import os
import sys
import runpy
from pathlib import Path


def _main() -> None:
    repo_root = Path(__file__).resolve().parent
    real_main = repo_root / "SH_discord_bot_split" / "main.py"

    if not real_main.exists():
        raise SystemExit(f"Entrypoint not found: {real_main}")

    # Ensure project modules in SH_discord_bot_split can be imported (e.g. `from config import TOKEN`).
    bot_dir = str(real_main.parent)
    if bot_dir not in sys.path:
        sys.path.insert(0, bot_dir)

    # Ensure relative paths inside the project behave as expected.
    os.chdir(repo_root)

    # Execute the real entrypoint as a script.
    runpy.run_path(str(real_main), run_name="__main__")


if __name__ == "__main__":
    _main()
