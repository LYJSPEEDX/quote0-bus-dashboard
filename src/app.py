"""AWS Lambda handler for the Sydney 526 Quote/0 departure board."""

from __future__ import annotations

import base64
import json
import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, time, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont


LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)

TFNSW_DEPARTURES_URL = "https://api.transport.nsw.gov.au/v1/tp/departure_mon"
QUOTE0_IMAGE_URL = "https://dot.mindreset.tech/api/authV2/open/device/{device_id}/image"
SCREEN_SIZE = (296, 152)
FONT_DIR = Path(__file__).resolve().parent / "fonts"


class ConfigurationError(ValueError):
    """Raised when the Lambda environment is incomplete or invalid."""


class UpstreamError(RuntimeError):
    """Raised when TfNSW or Quote/0 rejects a request."""


@dataclass(frozen=True)
class Departure:
    due_at: datetime
    minutes: int
    destination: str | None = None


@dataclass(frozen=True)
class Stop:
    """One platform of a location; its label names the direction of travel."""

    stop_id: str
    label: str


@dataclass(frozen=True)
class Location:
    """A screen: one route at up to two facing stops, pushed to its own Quote/0 task."""

    name: str
    route: str
    stops: tuple[Stop, ...]
    task_key: str | None = None


DEFAULT_LOCATIONS = json.dumps(
    [
        {
            "name": "Olympic Park",
            "route": "526",
            "stops": [{"id": "212726", "label": "Strathfield"}, {"id": "212727", "label": "Rhodes"}],
        }
    ]
)


@dataclass(frozen=True)
class Settings:
    tfnsw_api_key: str
    quote0_api_key: str
    quote0_device_id: str
    locations: tuple[Location, ...]
    max_departures: int
    timezone_name: str
    active_start: time
    active_end: time
    normal_refresh_minutes: int
    peak_windows: tuple[tuple[time, time], ...]
    peak_refresh_minutes: int

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    @classmethod
    def from_environment(cls, env: dict[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env

        def required(name: str) -> str:
            value = env.get(name, "").strip()
            if not value:
                raise ConfigurationError(f"Missing required environment variable: {name}")
            return value

        max_departures = _positive_int(env.get("MAX_DEPARTURES", "3"), "MAX_DEPARTURES")
        if max_departures > 3:
            raise ConfigurationError("MAX_DEPARTURES must be between 1 and 3")

        normal = _positive_int(env.get("NORMAL_REFRESH_MINUTES", "10"), "NORMAL_REFRESH_MINUTES")
        peak = _positive_int(env.get("PEAK_REFRESH_MINUTES", "2"), "PEAK_REFRESH_MINUTES")
        # The EventBridge schedule invokes every two minutes. Restrict intervals so a
        # configured refresh minute is never skipped by the scheduler.
        if normal % 2 or peak % 2:
            raise ConfigurationError("Refresh intervals must be positive multiples of 2 minutes")

        timezone_name = env.get("TIMEZONE", "Australia/Sydney").strip()
        try:
            ZoneInfo(timezone_name)
        except Exception as exc:  # pragma: no cover - platform-specific exception type
            raise ConfigurationError(f"Invalid TIMEZONE: {timezone_name}") from exc

        return cls(
            tfnsw_api_key=required("TFNSW_API_KEY"),
            quote0_api_key=required("QUOTE0_API_KEY"),
            quote0_device_id=required("QUOTE0_DEVICE_ID"),
            locations=_parse_locations(env.get("LOCATIONS") or DEFAULT_LOCATIONS),
            max_departures=max_departures,
            timezone_name=timezone_name,
            active_start=_parse_time(env.get("ACTIVE_START", "10:00"), "ACTIVE_START"),
            active_end=_parse_time(env.get("ACTIVE_END", "19:00"), "ACTIVE_END"),
            normal_refresh_minutes=normal,
            peak_windows=_parse_windows(env.get("PEAK_WINDOWS", "16:30-18:30")),
            peak_refresh_minutes=peak,
        )


def _optional(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _positive_int(value: str, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if parsed <= 0:
        raise ConfigurationError(f"{name} must be greater than zero")
    return parsed


def _parse_time(value: str, name: str) -> time:
    try:
        return time.fromisoformat(value.strip())
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be HH:MM") from exc


def _parse_locations(value: str) -> tuple[Location, ...]:
    """Parse LOCATIONS JSON: [{"name", "route", "task_key"?, "stops": [{"id", "label"}]}]."""
    try:
        raw = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ConfigurationError("LOCATIONS must be valid JSON") from exc
    if not isinstance(raw, list) or not raw:
        raise ConfigurationError("LOCATIONS must be a non-empty JSON list")

    locations: list[Location] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ConfigurationError("Each LOCATIONS entry must be an object")
        name, route = str(item.get("name", "")).strip(), str(item.get("route", "")).strip()
        if not name or not route:
            raise ConfigurationError("Each location needs a name and a route")
        stops_raw = item.get("stops")
        # The 296x152 panel fits two readable rows: one per direction.
        if not isinstance(stops_raw, list) or not 1 <= len(stops_raw) <= 2:
            raise ConfigurationError(f"Location {name} must list one or two stops")
        stops = []
        for stop in stops_raw:
            stop_id = str((stop or {}).get("id", "")).strip() if isinstance(stop, dict) else ""
            label = str(stop.get("label", "")).strip() if isinstance(stop, dict) else ""
            if not stop_id or not label:
                raise ConfigurationError(f"Location {name} stops need an id and a label")
            stops.append(Stop(stop_id=stop_id, label=label))
        locations.append(Location(name, route, tuple(stops), _optional(item.get("task_key"))))

    # Without distinct task keys, every location would overwrite the same Quote/0 task.
    if len(locations) > 1:
        keys = [location.task_key for location in locations]
        if None in keys or len(set(keys)) != len(keys):
            raise ConfigurationError("With several locations, each needs a unique task_key")
    return tuple(locations)


def _parse_windows(value: str) -> tuple[tuple[time, time], ...]:
    windows: list[tuple[time, time]] = []
    for item in (part.strip() for part in value.split(",")):
        if not item:
            continue
        try:
            start_text, end_text = item.split("-", maxsplit=1)
        except ValueError as exc:
            raise ConfigurationError("PEAK_WINDOWS must use HH:MM-HH:MM pairs") from exc
        start, end = _parse_time(start_text, "PEAK_WINDOWS"), _parse_time(end_text, "PEAK_WINDOWS")
        if start >= end:
            raise ConfigurationError("Peak-window end must be after its start")
        windows.append((start, end))
    return tuple(windows)


def should_refresh(now: datetime, settings: Settings) -> bool:
    """Return whether this scheduler tick is due to fetch and render data."""
    local_now = now.astimezone(settings.tz)
    current_time = local_now.timetz().replace(tzinfo=None)
    if not settings.active_start <= current_time < settings.active_end:
        return False

    minutes_since_midnight = local_now.hour * 60 + local_now.minute
    for start, end in settings.peak_windows:
        if start <= current_time < end:
            start_minutes = start.hour * 60 + start.minute
            return (minutes_since_midnight - start_minutes) % settings.peak_refresh_minutes == 0

    active_start_minutes = settings.active_start.hour * 60 + settings.active_start.minute
    return (minutes_since_midnight - active_start_minutes) % settings.normal_refresh_minutes == 0


def fetch_departures(
    settings: Settings,
    stop_id: str,
    route: str,
    now: datetime,
    opener: Callable[..., Any] = urlopen,
) -> list[Departure]:
    """Fetch, filter, and normalize the upcoming route departures from one stop."""
    local_now = now.astimezone(settings.tz)
    query = urlencode(
        {
            "outputFormat": "rapidJSON",
            "coordOutputFormat": "EPSG:4326",
            "mode": "direct",
            "type_dm": "stop",
            "name_dm": stop_id,
            "depArrMacro": "dep",
            "itdDate": local_now.strftime("%Y%m%d"),
            "itdTime": local_now.strftime("%H%M"),
            "TfNSWDM": "true",
            "version": "10.2.1.42",
            "departureMonitorMacro": "true",
        }
    )
    request = Request(
        f"{TFNSW_DEPARTURES_URL}?{query}",
        headers={"Authorization": f"apikey {settings.tfnsw_api_key}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with opener(request, timeout=12) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise UpstreamError(f"TfNSW departure request failed: {exc}") from exc

    events = payload.get("stopEvents", [])
    if not isinstance(events, list):
        raise UpstreamError("TfNSW response has no stopEvents list")

    now_utc = now.astimezone(timezone.utc)
    departures: list[Departure] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        transport = event.get("transportation") or {}
        if str(transport.get("number", "")) != route:
            continue
        destination = _destination(event, transport)
        due_at = _event_time(event)
        if due_at is None or due_at < now_utc:
            continue
        seconds = (due_at - now_utc).total_seconds()
        departures.append(Departure(due_at=due_at, minutes=max(0, math.ceil(seconds / 60)), destination=destination or None))

    departures.sort(key=lambda item: item.due_at)
    return departures[: settings.max_departures]


def _event_time(event: dict[str, Any]) -> datetime | None:
    # RapidJSON field names have changed across TfNSW API revisions. The first
    # two fields are the current specification; the latter two keep old fixtures
    # and responses compatible without weakening ETA-first behaviour.
    for field in ("estimatedTimeGMT", "plannedTimeGMT", "departureTimeEstimated", "departureTimePlanned"):
        raw = event.get(field)
        if not raw:
            continue
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            LOG.warning("Skipping malformed TfNSW timestamp field=%s", field)
    return None


def _destination(event: dict[str, Any], transport: dict[str, Any]) -> str:
    for source in (transport.get("destination"), event.get("destination")):
        if isinstance(source, dict):
            for key in ("name", "label"):
                if source.get(key):
                    return str(source[key])
        if isinstance(source, str):
            return source
    return ""


# Front view of a bus, 16x13: roof, windscreen, headlights, wheels.
BUS_ICON = (
    "..############..",
    ".##############.",
    ".##..........##.",
    ".##..........##.",
    ".##..........##.",
    ".##..........##.",
    ".##############.",
    ".##############.",
    ".#..########..#.",
    ".#..########..#.",
    ".##############.",
    "..###......###..",
    "..###......###..",
)
HEADER_HEIGHT = 18
ROW_HEIGHT = 67


def render_board(
    location: Location,
    boards: Sequence[tuple[Stop, Sequence[Departure]]],
    updated_at: datetime,
    settings: Settings,
) -> bytes:
    """Render a location as a header bar plus one row per stop, as a 1-bit PNG."""
    local_updated = updated_at.astimezone(settings.tz)
    image = Image.new("1", SCREEN_SIZE, 1)
    draw = ImageDraw.Draw(image)
    width, height = SCREEN_SIZE

    draw.rectangle((0, 0, width, HEADER_HEIGHT - 1), fill=0)
    _draw_icon(draw, BUS_ICON, 5, 3)
    header_font = _font(12, bold=True)
    draw.text((26, 2), f"{location.route} {location.name}", font=header_font, fill=1)
    stamp = local_updated.strftime("%H:%M")
    draw.text((width - 6 - _text_width(draw, stamp, header_font), 2), stamp, font=header_font, fill=1)

    row_height = (height - HEADER_HEIGHT) // max(1, len(boards))
    for index, (stop, departures) in enumerate(boards):
        top = HEADER_HEIGHT + index * row_height
        if index:
            draw.line((0, top, width, top), fill=0, width=2)
        # A single-stop location gets one tall row; keep its content vertically centred.
        _draw_row(draw, stop, list(departures), top + (row_height - ROW_HEIGHT) // 2, settings)
    return _png_bytes(image)


def _draw_icon(draw: ImageDraw.ImageDraw, rows: Sequence[str], left: int, top: int) -> None:
    for y, row in enumerate(rows):
        for x, cell in enumerate(row):
            if cell == "#":
                draw.point((left + x, top + y), fill=1)


def _draw_row(
    draw: ImageDraw.ImageDraw,
    stop: Stop,
    departures: list[Departure],
    top: int,
    settings: Settings,
) -> None:
    """Direction label and the next ETA on the left; following ETAs on the right."""
    width = SCREEN_SIZE[0]
    label_font = _font(13, bold=True)
    unit_font = _font(14, bold=True)
    draw.text((6, top + 3), f"→ {stop.label}".upper(), font=label_font, fill=0)

    if not departures:
        draw.text((6, top + 26), "No buses", font=_font(22, bold=True), fill=0)
        return

    first = departures[0]
    hero = "Now" if first.minutes == 0 else str(first.minutes)
    hero_font = _fit_font(draw, hero, 46, 118)
    hero_top = top + 15
    draw.text((4, hero_top), hero, font=hero_font, fill=0)
    if first.minutes:
        hero_right = 4 + _text_width(draw, hero, hero_font)
        draw.text((hero_right + 3, hero_top + 30), "min", font=unit_font, fill=0)

    # Right column: departure clock time of the next bus, then later ETAs.
    right = width - 6
    clock_font = _font(13, bold=False)
    clock = f"at {first.due_at.astimezone(settings.tz).strftime('%H:%M')}"
    draw.text((right - _text_width(draw, clock, clock_font), top + 4), clock, font=clock_font, fill=0)
    later = [str(item.minutes) for item in departures[1:]]
    if later:
        later_text = "  ".join(later)
        later_font = _fit_font(draw, later_text, 26, 120)
        later_width = _text_width(draw, later_text, later_font)
        draw.text((right - later_width, top + 22), later_text, font=later_font, fill=0)
        caption = "then (min)"
        caption_font = _font(10, bold=False)
        draw.text((right - _text_width(draw, caption, caption_font), top + 50), caption, font=caption_font, fill=0)


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    left, _top, right, _bottom = draw.textbbox((0, 0), text, font=font)
    return right - left


def _font(size: int, *, bold: bool) -> ImageFont.ImageFont:
    """Load the DejaVu font shipped in fonts/; Lambda has no system fonts."""
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(str(FONT_DIR / name), size)
    except OSError:
        LOG.warning("Bundled font %s missing; using Pillow default", name)
        return ImageFont.load_default(size=size)


def _fit_font(draw: ImageDraw.ImageDraw, text: str, size: int, max_width: int) -> ImageFont.ImageFont:
    """Shrink a bold font until the text fits, so two- and three-digit ETAs stay on screen."""
    while size > 12:
        font = _font(size, bold=True)
        left, _top, right, _bottom = draw.textbbox((0, 0), text, font=font)
        if right - left <= max_width:
            return font
        size -= 2
    return _font(size, bold=True)


def _png_bytes(image: Image.Image) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def push_image(
    settings: Settings,
    png: bytes,
    task_key: str | None = None,
    opener: Callable[..., Any] = urlopen,
) -> None:
    """Push a complete image to Quote/0's supported v2 Image API."""
    payload: dict[str, Any] = {
        "refreshNow": True,
        "image": base64.b64encode(png).decode("ascii"),
        "border": 0,
        "ditherType": "NONE",
    }
    if task_key:
        payload["taskKey"] = task_key
    request = Request(
        QUOTE0_IMAGE_URL.format(device_id=settings.quote0_device_id),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {settings.quote0_api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener(request, timeout=12) as response:
            body = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        hint = ""
        if exc.code == 404:
            hint = f" (no Image API task{f' with task_key {task_key!r}' if task_key else ''} in the device loop; add one in Dot App)"
        raise UpstreamError(f"Quote/0 image request failed: {exc}{hint}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise UpstreamError(f"Quote/0 image request failed: {exc}") from exc
    if int(body.get("code", 200)) >= 400:
        raise UpstreamError(f"Quote/0 image request rejected: code={body.get('code')}")


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """EventBridge Scheduler entry point. Failed calls intentionally preserve the screen."""
    settings = Settings.from_environment()
    now = datetime.now(timezone.utc)
    force_refresh = bool((event or {}).get("force_refresh"))
    if not force_refresh and not should_refresh(now, settings):
        LOG.info("Refresh skipped outside configured cadence")
        return {"status": "skipped"}

    results: dict[str, dict[str, int]] = {}
    failed: list[str] = []
    # Locations are independent Quote/0 tasks: one failing keeps its last image
    # while the others still refresh.
    for location in settings.locations:
        try:
            boards = [
                (stop, fetch_departures(settings, stop.stop_id, location.route, now)) for stop in location.stops
            ]
            push_image(settings, render_board(location, boards, now, settings), location.task_key)
        except UpstreamError:
            LOG.exception("Refresh failed for %s; retaining its last successful image", location.name)
            failed.append(location.name)
            continue
        results[location.name] = {stop.label: len(departures) for stop, departures in boards}

    LOG.info("Board updated departures=%s forced=%s", results, force_refresh)
    if failed:
        raise UpstreamError(f"Refresh failed for: {', '.join(failed)}")
    return {"status": "updated", "departures": results, "forced": force_refresh}
