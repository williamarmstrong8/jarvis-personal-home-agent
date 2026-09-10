import sys
import types
import unittest
from unittest.mock import patch


try:
    import httpx  # noqa: F401
except ImportError:
    httpx = types.ModuleType("httpx")
    sys.modules["httpx"] = httpx

try:
    import dotenv  # noqa: F401
except ImportError:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *args, **kwargs: None
    sys.modules["dotenv"] = dotenv

from integrations import pi_display


class SpotifyPushTests(unittest.TestCase):
    def setUp(self):
        self.previous_timestamp = pi_display._last_spotify_timestamp_ms
        pi_display._last_spotify_timestamp_ms = 0

    def tearDown(self):
        pi_display._last_spotify_timestamp_ms = self.previous_timestamp

    def test_spotify_posts_have_strictly_ordered_source_timestamps(self):
        payloads = []
        item = {
            "id": "track",
            "name": "Track",
            "artists": [{"name": "Artist"}],
            "album": {"images": []},
            "duration_ms": 120_000,
        }
        with (
            patch.object(pi_display.time, "time_ns", return_value=1_000_000_000),
            patch.object(pi_display, "post", side_effect=payloads.append),
        ):
            pi_display.push_spotify(item, progress_ms=100)
            pi_display.push_spotify(item, progress_ms=200)

        self.assertEqual(1000, payloads[0]["source_updated_at_ms"])
        self.assertEqual(1001, payloads[1]["source_updated_at_ms"])
        self.assertEqual([100, 200], [p["progress_ms"] for p in payloads])


if __name__ == "__main__":
    unittest.main()
