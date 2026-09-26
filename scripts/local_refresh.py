#!/usr/bin/env python3
"""Run one board refresh locally using credentials from .env.

    python scripts/local_refresh.py            # fetch TfNSW, render board-*.png only
    python scripts/local_refresh.py --push     # also push each location to Quote/0

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
    parser.add_argument("--push", action="store_true", help="push the rendered images to Quote/0")
    parser.add_argument("--out-dir", default=str(PROJECT_DIR), help="where to write board-<n>.png files")
    args = parser.parse_args()

    load_dotenv(PROJECT_DIR / ".env")
    env = dict(os.environ)
    if not args.push:
        # Rendering alone does not need Quote/0 credentials.
        for key in ("QUOTE0_API_KEY", "QUOTE0_DEVICE_ID"):
            env[key] = env.get(key) or "unused"
    settings = app.Settings.from_environment(env)

    now = datetime.now(timezone.utc)
    for index, location in enumerate(settings.locations, start=1):
        print(f"[{location.route} {location.name}] task_key={location.task_key or '-'}")
        boards = [
            (stop, app.fetch_departures(settings, stop.stop_id, location.route, now)) for stop in location.stops
        ]
        for stop, departures in boards:
            print(f"  -> {stop.label:<12} {stop.stop_id}: {[item.minutes for item in departures]} min")

        png = app.render_board(location, boards, now, settings)
        out = Path(args.out_dir) / f"board-{index}.png"
        out.write_bytes(png)
        print(f"  wrote {out}")
        if args.push:
            app.push_image(settings, png, location.task_key)
            print("  pushed to Quote/0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
