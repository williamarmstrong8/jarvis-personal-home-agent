import importlib.util
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch


def load_display_module():
    numpy = types.ModuleType("numpy")
    pil = types.ModuleType("PIL")
    pil.Image = types.ModuleType("PIL.Image")
    pil.ImageDraw = types.ModuleType("PIL.ImageDraw")
    pil.ImageFont = types.ModuleType("PIL.ImageFont")
    path = Path(__file__).parents[1] / "tools" / "pi-display" / "display.py"
    spec = importlib.util.spec_from_file_location("test_pi_display_service", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"numpy": numpy, "PIL": pil}):
        assert spec.loader is not None
        spec.loader.exec_module(module)
    return module


class PiDisplayStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.display = load_display_module()

    def setUp(self):
        d = self.display
        with d.LK:
            d.S.update({
                "mode": "idle",
                "playing": False,
                "anchor_ms": 0,
                "anchor_t": 0.0,
                "dur": 0,
                "track": None,
                "item": None,
                "base": None,
                "full_pending": False,
                "source_updated_at_ms": 0,
                "render_generation": 0,
                "last_remote_push": 0.0,
            })

    def _item(self, track_id):
        return {
            "id": track_id,
            "name": track_id,
            "artists": [],
            "album": {"images": []},
            "duration_ms": 100_000,
        }

    def test_reordered_http_push_cannot_restore_an_old_track(self):
        d = self.display
        ready = threading.Event()

        def frame(item, _pa, fetch_art=True):
            ready.set()
            return item["id"].encode()

        with (
            patch.object(d, "stop_video"),
            patch.object(d, "publish"),
            patch.object(d, "base_frame", side_effect=frame),
            patch.object(d, "to565", side_effect=lambda value: value),
            patch.object(d.os, "remove", side_effect=FileNotFoundError),
        ):
            d.apply_spotify(self._item("new"), source_updated_at_ms=200)
            self.assertTrue(ready.wait(1))
            d.apply_spotify(self._item("old"), source_updated_at_ms=100)

        self.assertEqual("new", d.S["track"])

    def test_slow_old_artwork_cannot_overwrite_a_new_track(self):
        d = self.display
        old_started = threading.Event()
        release_old = threading.Event()
        new_ready = threading.Event()

        def frame(item, _pa, fetch_art=True):
            if item["id"] == "old":
                old_started.set()
                release_old.wait(1)
            else:
                new_ready.set()
            return item["id"].encode()

        with (
            patch.object(d, "stop_video"),
            patch.object(d, "publish"),
            patch.object(d, "base_frame", side_effect=frame),
            patch.object(d, "to565", side_effect=lambda value: value),
            patch.object(d.os, "remove", side_effect=FileNotFoundError),
        ):
            d.apply_spotify(self._item("old"), source_updated_at_ms=100)
            self.assertTrue(old_started.wait(1))
            d.apply_spotify(self._item("new"), source_updated_at_ms=200)
            self.assertTrue(new_ready.wait(1))
            release_old.set()
            old_thread_finished = threading.Event()
            threading.Timer(0.03, old_thread_finished.set).start()
            self.assertTrue(old_thread_finished.wait(1))

        self.assertEqual("new", d.S["track"])
        self.assertEqual(b"new", d.S["base"])

    def test_status_progress_uses_the_monotonic_anchor(self):
        d = self.display
        with d.LK:
            d.S.update({
                "playing": True,
                "anchor_ms": 10_000,
                "anchor_t": 50.0,
                "dur": 120_000,
                "item": {"name": "Track"},
            })
        with (
            patch.object(d.time, "monotonic", return_value=52.5),
            patch.object(d, "_fb_blank", return_value="0"),
        ):
            status = d._status(full=True)

        self.assertEqual(12_500, status["spotify_progress_ms"])
        self.assertEqual("Track", status["spotify_track"])


if __name__ == "__main__":
    unittest.main()
