import base64
import json
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app


ENV = {
    "TFNSW_API_KEY": "tfnsw-secret",
    "QUOTE0_API_KEY": "quote-secret",
    "QUOTE0_DEVICE_ID": "DEVICE123",
}


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class AppTests(unittest.TestCase):
    def setUp(self):
        self.settings = app.Settings.from_environment(ENV)
        self.now = datetime(2026, 9, 16, 6, 0, tzinfo=timezone.utc)  # 16:00 Sydney

    def test_refresh_schedule_boundaries(self):
        at = lambda hour, minute: datetime(2026, 9, 16, hour, minute, tzinfo=self.settings.tz)
        self.assertTrue(app.should_refresh(at(10, 0), self.settings))
        self.assertFalse(app.should_refresh(at(10, 2), self.settings))
        self.assertTrue(app.should_refresh(at(10, 30), self.settings))
        self.assertTrue(app.should_refresh(at(16, 30), self.settings))
        self.assertFalse(app.should_refresh(at(16, 32), self.settings))
        self.assertFalse(app.should_refresh(at(17, 0), self.settings))

        peak = app.Settings.from_environment({**ENV, "PEAK_WINDOWS": "16:00-16:30", "PEAK_REFRESH_MINUTES": "2"})
        self.assertTrue(app.should_refresh(at(16, 2), peak))
        self.assertFalse(app.should_refresh(at(15, 2), peak))

    def test_fetch_prefers_estimate_filters_and_sorts(self):
        captured = {}

        def opener(request, timeout):
            captured["request"] = request
            return FakeResponse(
                {
                    "stopEvents": [
                        {"transportation": {"number": "526"}, "estimatedTimeGMT": "2026-09-16T06:20:00Z"},
                        {"transportation": {"number": "526"}, "estimatedTimeGMT": "2026-09-16T06:05:01Z", "plannedTimeGMT": "2026-09-16T08:00:00Z"},
                        {"transportation": {"number": "526"}, "plannedTimeGMT": "2026-09-16T06:12:00Z"},
                        {"transportation": {"number": "525"}, "estimatedTimeGMT": "2026-09-16T06:01:00Z"},
                        {"transportation": {"number": "526"}, "estimatedTimeGMT": "2026-09-16T05:59:00Z"},
                    ]
                }
            )

        result = app.fetch_departures(self.settings, "212726", "526", self.now, opener)
        self.assertEqual([item.minutes for item in result], [6, 12, 20])
        parsed = parse_qs(urlparse(captured["request"].full_url).query)
        self.assertEqual(parsed["name_dm"], ["212726"])
        self.assertEqual(captured["request"].get_header("Authorization"), "apikey tfnsw-secret")

    def test_default_location_pairs_both_directions(self):
        (location,) = self.settings.locations
        self.assertEqual((location.name, location.route, location.task_key), ("Olympic Park", "526", None))
        self.assertEqual(location.stops, (app.Stop("212726", "Strathfield"), app.Stop("212727", "Rhodes")))

    def test_locations_config_validation(self):
        stops = [{"id": "1", "label": "North"}]
        valid = [
            {"name": "A", "route": "526", "task_key": "a", "stops": stops},
            {"name": "B", "route": "533", "task_key": "b", "stops": stops},
        ]
        settings = app.Settings.from_environment({**ENV, "LOCATIONS": json.dumps(valid)})
        self.assertEqual([location.task_key for location in settings.locations], ["a", "b"])
        invalid = [
            "not json",
            "[]",
            json.dumps([{"name": "A", "route": "526", "stops": stops * 3}]),
            json.dumps([{"name": "A", "route": "526", "stops": [{"id": "1"}]}]),
            json.dumps([{**valid[0], "task_key": ""}, valid[1]]),
            json.dumps([valid[0], {**valid[1], "task_key": "a"}]),
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(app.ConfigurationError):
                app.Settings.from_environment({**ENV, "LOCATIONS": value})

    def test_png_output_for_two_and_one_stop_locations(self):
        location = self.settings.locations[0]
        departures = [app.Departure(self.now, 0), app.Departure(self.now, 118)]
        single = app.Location("Rhodes", "533", (app.Stop("1", "Chatswood"),), "rhodes")
        for boards, loc in (
            ([(location.stops[0], departures), (location.stops[1], [])], location),
            ([(single.stops[0], departures)], single),
        ):
            image = app.Image.open(app.BytesIO(app.render_board(loc, boards, self.now, self.settings)))
            self.assertEqual(image.size, (296, 152))
            self.assertEqual(image.mode, "1")

    def test_bundled_fonts_are_used(self):
        self.assertIsInstance(app._font(12, bold=True), app.ImageFont.FreeTypeFont)
        self.assertTrue((app.FONT_DIR / "DejaVuSans-Bold.ttf").is_file())

    def test_rows_stay_within_the_screen(self):
        location = self.settings.locations[0]
        departures = [app.Departure(self.now, minutes) for minutes in (118, 128, 138)]
        png = app.render_board(location, [(stop, departures) for stop in location.stops], self.now, self.settings)
        image = app.Image.open(app.BytesIO(png))
        pixels = image.load()
        ink_rows = [y for y in range(image.height) if any(pixels[x, y] == 0 for x in range(image.width))]
        self.assertLess(max(ink_rows), image.height - 2)

    def test_long_eta_fits_on_screen(self):
        image = app.Image.new("1", app.SCREEN_SIZE, 1)
        draw = app.ImageDraw.Draw(image)
        for text in ("12", "105"):
            self.assertLessEqual(app._text_width(draw, text, app._fit_font(draw, text, 46, 118)), 118)

    def test_push_uses_v2_endpoint_task_key_and_no_dither(self):
        captured = {}

        def opener(request, timeout):
            captured["request"] = request
            return FakeResponse({"code": 200})

        app.push_image(self.settings, b"png", "bus-board", opener)
        request = captured["request"]
        self.assertIn("/DEVICE123/image", request.full_url)
        self.assertEqual(request.get_header("Authorization"), "Bearer quote-secret")
        body = json.loads(request.data)
        self.assertEqual(body["taskKey"], "bus-board")
        self.assertEqual(body["ditherType"], "NONE")
        self.assertEqual(base64.b64decode(body["image"]), b"png")

    def test_upstream_error_does_not_produce_a_replacement_image(self):
        def offline(_request, timeout):
            raise URLError("offline")

        with self.assertRaises(app.UpstreamError):
            app.fetch_departures(self.settings, "212726", "526", self.now, offline)

        location = self.settings.locations[0]
        empty_png = app.render_board(location, [(stop, []) for stop in location.stops], self.now, self.settings)
        image = app.Image.open(app.BytesIO(empty_png))
        self.assertEqual(image.size, (296, 152))

    def test_handler_force_refresh_bypasses_time_gate(self):
        previous = dict(os.environ)
        os.environ.update(ENV)
        original_fetch = app.fetch_departures
        original_render = app.render_board
        original_push = app.push_image
        try:
            app.fetch_departures = lambda *_args: []
            app.render_board = lambda *_args: b"png"
            app.push_image = lambda *_args: None
            result = app.lambda_handler({"force_refresh": True}, None)
        finally:
            app.fetch_departures = original_fetch
            app.render_board = original_render
            app.push_image = original_push
            os.environ.clear()
            os.environ.update(previous)
        self.assertEqual(result["status"], "updated")
        self.assertTrue(result["forced"])
        self.assertEqual(result["departures"], {"Olympic Park": {"Strathfield": 0, "Rhodes": 0}})

    def test_handler_keeps_refreshing_other_locations_when_one_fails(self):
        stops = [{"id": "1", "label": "North"}]
        locations = [
            {"name": "A", "route": "526", "task_key": "a", "stops": stops},
            {"name": "B", "route": "533", "task_key": "b", "stops": stops},
        ]
        previous = dict(os.environ)
        os.environ.update({**ENV, "LOCATIONS": json.dumps(locations)})
        pushed = []
        original_fetch, original_push = app.fetch_departures, app.push_image

        def fetch(_settings, _stop_id, route, _now):
            if route == "526":
                raise app.UpstreamError("offline")
            return []

        try:
            app.fetch_departures = fetch
            app.push_image = lambda _settings, _png, task_key: pushed.append(task_key)
            with self.assertRaises(app.UpstreamError):
                app.lambda_handler({"force_refresh": True}, None)
        finally:
            app.fetch_departures, app.push_image = original_fetch, original_push
            os.environ.clear()
            os.environ.update(previous)
        self.assertEqual(pushed, ["b"])


if __name__ == "__main__":
    unittest.main()
