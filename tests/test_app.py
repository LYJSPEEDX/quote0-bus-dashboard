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
        self.assertTrue(app.should_refresh(at(16, 30), self.settings))
        self.assertTrue(app.should_refresh(at(16, 32), self.settings))
        self.assertFalse(app.should_refresh(at(19, 0), self.settings))

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

        result = app.fetch_departures(self.settings, "212726", self.now, opener)
        self.assertEqual([item.minutes for item in result], [6, 12, 20])
        parsed = parse_qs(urlparse(captured["request"].full_url).query)
        self.assertEqual(parsed["name_dm"], ["212726"])
        self.assertEqual(captured["request"].get_header("Authorization"), "apikey tfnsw-secret")

    def test_default_stops_cover_both_directions(self):
        self.assertEqual(
            self.settings.directions,
            (app.Direction("212726", "Strathfield"), app.Direction("212727", "Rhodes")),
        )
        with self.assertRaises(app.ConfigurationError):
            app.Settings.from_environment({**ENV, "STOPS": "212726"})
        with self.assertRaises(app.ConfigurationError):
            app.Settings.from_environment({**ENV, "STOPS": "1:A,2:B,3:C"})

    def test_two_direction_png_output(self):
        departures = [app.Departure(self.now, 0), app.Departure(self.now, 118)]
        boards = [(self.settings.directions[0], departures), (self.settings.directions[1], [])]
        png = app.render_board(boards, self.now, self.settings)
        image = app.Image.open(app.BytesIO(png))
        self.assertEqual(image.size, (296, 152))
        self.assertEqual(image.mode, "1")

    def test_bundled_fonts_are_used(self):
        self.assertIsInstance(app._font(12, bold=True), app.ImageFont.FreeTypeFont)
        self.assertTrue((app.FONT_DIR / "DejaVuSans-Bold.ttf").is_file())

    def test_long_eta_fits_on_screen(self):
        image = app.Image.new("1", app.SCREEN_SIZE, 1)
        draw = app.ImageDraw.Draw(image)
        for minutes in (12, 105):
            text = f"NEXT: {minutes} min"
            left, _top, right, _bottom = draw.textbbox((0, 0), text, font=app._fit_font(draw, text, 42, 276))
            self.assertLessEqual(right - left, 276)

    def test_push_uses_v2_endpoint_task_key_and_no_dither(self):
        settings = app.Settings.from_environment({**ENV, "QUOTE0_TASK_KEY": "bus-board"})
        captured = {}

        def opener(request, timeout):
            captured["request"] = request
            return FakeResponse({"code": 200})

        app.push_image(settings, b"png", opener)
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
            app.fetch_departures(self.settings, "212726", self.now, offline)

        empty_png = app.render_board([(direction, []) for direction in self.settings.directions], self.now, self.settings)
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


if __name__ == "__main__":
    unittest.main()
