import threading
import sys
import types
import unittest
from unittest.mock import patch

from core import streaming_stt
from core.streaming_stt import RollingTranscriber


def pcm_frame(samples: int, value: int = 1) -> bytes:
    return int(value).to_bytes(2, "little", signed=True) * samples


class RollingTranscriberTests(unittest.TestCase):
    def test_auto_mode_requires_accelerated_backend(self):
        speech = types.ModuleType("core.speech")
        speech.selected_stt_backend = lambda: "openai-whisper"
        with (
            patch.dict(sys.modules, {"core.speech": speech}),
            patch.object(streaming_stt, "STREAMING_MODE", "auto"),
        ):
            self.assertIsNone(streaming_stt.create_streaming_transcriber())

    def test_completed_partial_covering_last_speech_is_reused(self):
        calls = []
        partial_ready = threading.Event()

        def transcribe(pcm):
            calls.append(len(pcm))
            return "check the pi status"

        session = RollingTranscriber(
            transcribe,
            sample_rate=100,
            on_partial=lambda _text, _stable: partial_ready.set(),
            partial_interval_secs=0.1,
            partial_min_audio_secs=0.1,
            preview_silence_secs=0.05,
            final_timeout_secs=1,
        )
        session.push(pcm_frame(10), is_speech=True)
        self.assertTrue(partial_ready.wait(1))

        result = session.finish()

        self.assertEqual("check the pi status", result)
        self.assertEqual(1, len(calls))

    def test_new_speech_invalidates_an_older_partial(self):
        calls = []
        first_ready = threading.Event()

        def transcribe(pcm):
            calls.append(len(pcm))
            first_ready.set()
            return f"audio bytes {len(pcm)}"

        session = RollingTranscriber(
            transcribe,
            sample_rate=100,
            partial_interval_secs=0.1,
            partial_min_audio_secs=0.1,
            preview_silence_secs=0.05,
            final_timeout_secs=1,
        )
        session.push(pcm_frame(10), is_speech=True)
        self.assertTrue(first_ready.wait(1))
        session.push(pcm_frame(5), is_speech=True)

        result = session.finish()

        self.assertEqual("audio bytes 30", result)
        self.assertEqual([20, 30], calls)

    def test_early_candidate_missing_the_final_tail_is_not_reused(self):
        calls = []
        first_ready = threading.Event()
        second_ready = threading.Event()
        partial_count = 0

        def transcribe(pcm):
            calls.append(len(pcm))
            return f"audio bytes {len(pcm)}"

        def on_partial(_text, _stable):
            nonlocal partial_count
            partial_count += 1
            if partial_count == 1:
                first_ready.set()
            elif partial_count == 2:
                second_ready.set()

        session = RollingTranscriber(
            transcribe,
            sample_rate=100,
            on_partial=on_partial,
            partial_interval_secs=0.1,
            partial_min_audio_secs=0.1,
            preview_silence_secs=0.05,
            endpoint_silence_secs=1.0,
            final_tail_tolerance_secs=0.05,
            final_timeout_secs=1,
        )
        session.push(pcm_frame(10), is_speech=True)
        self.assertTrue(first_ready.wait(1))
        session.push(pcm_frame(10), is_speech=False)
        self.assertTrue(second_ready.wait(1))
        session.push(pcm_frame(10), is_speech=False)

        result = session.finish()

        self.assertEqual("audio bytes 60", result)
        self.assertEqual([20, 40, 60], calls)

    def test_stability_requires_repeated_normalized_text(self):
        results = []
        first_ready = threading.Event()
        second_ready = threading.Event()

        def on_partial(text, stable):
            results.append((text, stable))
            if len(results) == 1:
                first_ready.set()
            elif len(results) == 2:
                second_ready.set()

        session = RollingTranscriber(
            lambda _pcm: "Pause the music.",
            sample_rate=100,
            on_partial=on_partial,
            partial_interval_secs=0.1,
            partial_min_audio_secs=0.1,
            preview_silence_secs=0.05,
            final_timeout_secs=1,
        )
        session.push(pcm_frame(10), is_speech=True)
        self.assertTrue(first_ready.wait(1))
        session.push(pcm_frame(10), is_speech=True)
        self.assertTrue(second_ready.wait(1))
        session.close()

        self.assertEqual([False, True], [stable for _text, stable in results[:2]])


if __name__ == "__main__":
    unittest.main()
