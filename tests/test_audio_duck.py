import sys
import types
import unittest
from unittest.mock import patch

from core import audio_duck


class AudioDuckTests(unittest.TestCase):
    def setUp(self):
        audio_duck._should_resume = False
        audio_duck._ducked = False

    def tearDown(self):
        audio_duck._should_resume = False
        audio_duck._ducked = False

    def test_duck_uses_single_fast_spotify_operation(self):
        calls = []
        spotify = types.ModuleType("integrations.spotify")

        def pause_for_voice():
            calls.append("pause")
            return True

        spotify.pause_for_voice = pause_for_voice
        with (
            patch.dict(sys.modules, {"integrations.spotify": spotify}),
            patch.object(audio_duck.time, "sleep"),
        ):
            self.assertTrue(audio_duck.duck())

        self.assertEqual(["pause"], calls)
        self.assertTrue(audio_duck.is_ducked())


if __name__ == "__main__":
    unittest.main()
