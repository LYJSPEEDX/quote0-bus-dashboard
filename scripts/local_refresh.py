#!/usr/bin/env python3
"""Run one board refresh locally using credentials from .env.

    python scripts/local_refresh.py            # fetch TfNSW, render board.png only
    python scripts/local_refresh.py --push     # also push the image to Quote/0

Secrets are read from the environment first, then from the git-ignored .env.
Nothing is printed except departure counts and the push result.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "src"))

import app  # noqa: E402


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--push", action="store_true", help="push the rendered image to Quote/0")
    parser.add_argument("--out", default=str(PROJECT_DIR / "board.png"), help="where to write the PNG")
    args = parser.parse_args()

    load_dotenv(PROJECT_DIR / ".env")
    env = dict(os.environ)
    if not args.push:
        # Rendering alone does not need Quote/0 credentials.
        for key in ("QUOTE0_API_KEY", "QUOTE0_DEVICE_ID"):
            env[key] = env.get(key) or "unused"
    settings = app.Settings.from_environment(env)

    now = datetime.now(timezone.utc)
    boards = [(direction, app.fetch_departures(settings, direction.stop_id, now)) for direction in settings.directions]
    for direction, departures in boards:
        print(f"{direction.label:<12} {direction.stop_id}: {[item.minutes for item in departures]} min")

    png = app.render_board(boards, now, settings)
    Path(args.out).write_bytes(png)
    print(f"Wrote {args.out}")

    if args.push:
        app.push_image(settings, png)
        print("Pushed to Quote/0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
