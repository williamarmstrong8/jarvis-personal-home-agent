import sys
import types
import unittest

import numpy as np


try:
    import pyaudio  # noqa: F401
except ImportError:
    sys.modules["pyaudio"] = types.ModuleType("pyaudio")

try:
    import dotenv  # noqa: F401
except ImportError:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda: None
    sys.modules["dotenv"] = dotenv

import main


def frame(level: int) -> bytes:
    return np.full(main.RECORD_CHUNK_SIZE, level, dtype=np.int16).tobytes()


class FakeStream:
    def __init__(self, frames):
        self.frames = iter(frames)

    def read(self, _size, exception_on_overflow=False):
        return next(self.frames)


class AdaptiveVadTests(unittest.TestCase):
    def setUp(self):
        self.old_floor = main._noise_floor_rms
        main._noise_floor_rms = 40

    def tearDown(self):
        main._noise_floor_rms = self.old_floor

    def test_threshold_uses_quiet_room_floor(self):
        self.assertEqual(main.VAD_MIN_RMS, main._vad_threshold())

    def test_stops_after_configured_post_speech_silence(self):
        silence_chunks = max(
            2, int(round(main.VAD_SILENCE_SECS * main.SAMPLE_RATE / main.RECORD_CHUNK_SIZE))
        )
        chunks = [frame(500), frame(500)] + [frame(0)] * (silence_chunks + 2)
        audio = main.record_until_silence(FakeStream(chunks), max_secs=3)
        expected_chunks = 2 + silence_chunks
        self.assertEqual(expected_chunks * main.RECORD_CHUNK_SIZE * 2, len(audio))

    def test_recent_seed_speech_allows_fast_endpoint(self):
        silence_chunks = max(
            2, int(round(main.VAD_SILENCE_SECS * main.SAMPLE_RATE / main.RECORD_CHUNK_SIZE))
        )
        seed = [frame(500)]
        audio = main.record_until_silence(
            FakeStream([frame(0)] * (silence_chunks + 1)),
            seed_frames=seed,
            max_secs=3,
        )
        self.assertEqual((1 + silence_chunks) * main.RECORD_CHUNK_SIZE * 2, len(audio))


if __name__ == "__main__":
    unittest.main()
