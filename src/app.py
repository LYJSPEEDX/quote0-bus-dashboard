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
from typing import Any, Callable, Iterable
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
class Settings:
    tfnsw_api_key: str
    quote0_api_key: str
    quote0_device_id: str
    quote0_task_key: str | None
    stop_id: str
    route_number: str
    destination_filter: str | None
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
            quote0_task_key=_optional(env.get("QUOTE0_TASK_KEY")),
            stop_id=env.get("STOP_ID", "212711").strip(),
            route_number=env.get("ROUTE_NUMBER", "526").strip(),
            destination_filter=_optional(env.get("DESTINATION_FILTER")),
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
    now: datetime,
    opener: Callable[..., Any] = urlopen,
) -> list[Departure]:
    """Fetch, filter, and normalize the upcoming configured route departures."""
    local_now = now.astimezone(settings.tz)
    query = urlencode(
        {
            "outputFormat": "rapidJSON",
            "coordOutputFormat": "EPSG:4326",
            "mode": "direct",
            "type_dm": "stop",
            "name_dm": settings.stop_id,
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
        if str(transport.get("number", "")) != settings.route_number:
            continue
        destination = _destination(event, transport)
        if settings.destination_filter and settings.destination_filter.casefold() not in destination.casefold():
            continue
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


def render_board(departures: Iterable[Departure], updated_at: datetime, settings: Settings) -> bytes:
    """Render the complete Quote/0 image as a 1-bit landscape PNG."""
    local_updated = updated_at.astimezone(settings.tz)
    items = list(departures)
    image = Image.new("1", SCREEN_SIZE, 1)
    draw = ImageDraw.Draw(image)
    title_font = _font(15, bold=True)
    hero_font = _font(42, bold=True)
    body_font = _font(15, bold=False)
    footer_font = _font(10, bold=False)

    draw.text((10, 8), "526 Olympic Park", font=title_font, fill=0)
    draw.line((10, 28, 286, 28), fill=0, width=1)

    if items:
        hero = f"NEXT: {items[0].minutes} min"
        _centered_text(draw, hero, hero_font, y=39)
        following = ", ".join(f"{item.minutes} min" for item in items[1:]) or "--"
        _centered_text(draw, f"THEN: {following}", body_font, y=96)
    else:
        _centered_text(draw, "NO 526 DEPARTURES", body_font, y=64)
        _centered_text(draw, "Check again soon", footer_font, y=87)

    footer = f"Updated {local_updated.strftime('%H:%M:%S')}"
    draw.text((10, 137), footer, font=footer_font, fill=0)
    return _png_bytes(image)


def _font(size: int, *, bold: bool) -> ImageFont.ImageFont:
    """Use bundled Pillow's scalable DejaVu font where present; fall back safely."""
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(name, size)
    except OSError:
        return ImageFont.load_default(size=size)


def _centered_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, y: int) -> None:
    left, _top, right, _bottom = draw.textbbox((0, 0), text, font=font)
    draw.text(((SCREEN_SIZE[0] - (right - left)) // 2, y), text, font=font, fill=0)


def _png_bytes(image: Image.Image) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def push_image(settings: Settings, png: bytes, opener: Callable[..., Any] = urlopen) -> None:
    """Push a complete image to Quote/0's supported v2 Image API."""
    payload: dict[str, Any] = {
        "refreshNow": True,
        "image": base64.b64encode(png).decode("ascii"),
        "border": 0,
        "ditherType": "NONE",
    }
    if settings.quote0_task_key:
        payload["taskKey"] = settings.quote0_task_key
    request = Request(
        QUOTE0_IMAGE_URL.format(device_id=settings.quote0_device_id),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {settings.quote0_api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener(request, timeout=12) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
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

    try:
        departures = fetch_departures(settings, now)
        png = render_board(departures, now, settings)
        push_image(settings, png)
    except UpstreamError:
        LOG.exception("Board refresh failed; retaining last successful image")
        raise

    LOG.info("Board updated departures=%s forced=%s", len(departures), force_refresh)
    return {"status": "updated", "departures": len(departures), "forced": force_refresh}
