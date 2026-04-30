from __future__ import annotations
import runpy
from pathlib import Path

# Compatibility entrypoint: if hosting runs this old file, load the updated split bot.
if __name__ == "__main__":
    real_main = Path(__file__).resolve().parent / "main.py"
    runpy.run_path(str(real_main), run_name="__main__")
