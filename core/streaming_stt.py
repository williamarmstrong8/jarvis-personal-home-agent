"""Safe rolling transcription for the wake-word audio pipeline.

whisper.cpp's CLI is file-oriented rather than statefully streaming. This
adapter still removes useful latency by transcribing rolling snapshots while
the user is speaking and, most importantly, starting a final candidate during
the endpoint-silence window. A candidate is reused only when its audio snapshot
contains both the last frame classified as speech and nearly all captured tail
audio; partial text never executes a tool.
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
import time
import wave
from collections.abc import Callable


STREAMING_MODE = os.environ.get("STT_STREAMING", "auto").strip().lower()
PARTIAL_INTERVAL_SECS = max(
    0.4, float(os.environ.get("STT_PARTIAL_INTERVAL_SECS", "0.9"))
)
PARTIAL_MIN_AUDIO_SECS = max(
    0.4, float(os.environ.get("STT_PARTIAL_MIN_AUDIO_SECS", "0.7"))
)
PREVIEW_SILENCE_SECS = max(
    0.06, float(os.environ.get("STT_PREVIEW_SILENCE_SECS", "0.12"))
)
FINAL_TIMEOUT_SECS = max(
    2.0, float(os.environ.get("STT_STREAMING_FINAL_TIMEOUT", "30"))
)
FINAL_TAIL_TOLERANCE_SECS = max(
    0.0, float(os.environ.get("STT_FINAL_TAIL_TOLERANCE_SECS", "0.09"))
)


def _normalized(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


class RollingTranscriber:
    """Latest-only background transcription with final-audio verification."""

    def __init__(
        self,
        transcribe_pcm: Callable[[bytes], str],
        *,
        sample_rate: int = 16000,
        on_partial: Callable[[str, bool], None] | None = None,
        trace=None,
        partial_interval_secs: float = PARTIAL_INTERVAL_SECS,
        partial_min_audio_secs: float = PARTIAL_MIN_AUDIO_SECS,
        preview_silence_secs: float = PREVIEW_SILENCE_SECS,
        endpoint_silence_secs: float = 0.36,
        final_tail_tolerance_secs: float = FINAL_TAIL_TOLERANCE_SECS,
        final_timeout_secs: float = FINAL_TIMEOUT_SECS,
    ):
        self._transcribe_pcm = transcribe_pcm
        self._sample_rate = sample_rate
        self._bytes_per_second = sample_rate * 2
        self._on_partial = on_partial
        self._trace = trace
        self._partial_interval = partial_interval_secs
        self._partial_min_audio = partial_min_audio_secs
        self._preview_silence = preview_silence_secs
        self._endpoint_silence = endpoint_silence_secs
        self._final_tail_tolerance = final_tail_tolerance_secs
        self._final_timeout = final_timeout_secs

        self._audio = bytearray()
        self._last_speech_end = 0
        self._silence_secs = 0.0
        self._last_requested_secs = 0.0
        self._early_previewed_speech_end = -1
        self._final_previewed_speech_end = -1
        self._sequence = 0
        self._pending: tuple[int, int, bytes] | None = None
        self._active: tuple[int, int] | None = None
        self._result: tuple[int, int, str] | None = None
        self._error_sequence = 0
        self._previous_normalized = ""
        self._partial_count = 0
        self._closed = False
        self._condition = threading.Condition()
        self._worker = threading.Thread(
            target=self._run,
            daemon=True,
            name="jarvis-streaming-stt",
        )
        self._worker.start()

    @property
    def partial_count(self) -> int:
        return self._partial_count

    def push(self, pcm: bytes, *, is_speech: bool) -> None:
        if not pcm:
            return
        with self._condition:
            if self._closed:
                return
            self._audio.extend(pcm)
            frame_secs = len(pcm) / self._bytes_per_second
            audio_secs = len(self._audio) / self._bytes_per_second
            if is_speech:
                self._last_speech_end = len(self._audio)
                self._silence_secs = 0.0
            elif self._last_speech_end:
                self._silence_secs += frame_secs

            periodic = (
                audio_secs >= self._partial_min_audio
                and audio_secs - self._last_requested_secs >= self._partial_interval
            )
            early_endpoint_preview = (
                self._last_speech_end > 0
                and self._silence_secs >= self._preview_silence
                and self._early_previewed_speech_end != self._last_speech_end
            )
            final_preview_at = max(
                self._preview_silence,
                self._endpoint_silence - self._final_tail_tolerance,
            )
            final_endpoint_preview = (
                self._last_speech_end > 0
                and self._silence_secs >= final_preview_at
                and self._final_previewed_speech_end != self._last_speech_end
            )
            if periodic or early_endpoint_preview or final_endpoint_preview:
                self._request_locked()
                if early_endpoint_preview:
                    self._early_previewed_speech_end = self._last_speech_end
                if final_endpoint_preview:
                    self._final_previewed_speech_end = self._last_speech_end

    def _request_locked(self) -> int:
        self._sequence += 1
        snapshot = bytes(self._audio)
        size = len(snapshot)
        self._pending = (self._sequence, size, snapshot)
        self._last_requested_secs = size / self._bytes_per_second
        if self._trace and "streaming_stt_started" not in self._trace.marks:
            self._trace.mark("streaming_stt_started")
            if "stt_started" not in self._trace.marks:
                self._trace.mark("stt_started")
        self._condition.notify_all()
        return self._sequence

    def finish(self) -> str | None:
        """Return a verified whole-utterance transcript, or None for fallback STT."""
        deadline = time.monotonic() + self._final_timeout
        with self._condition:
            tail_tolerance = int(
                self._final_tail_tolerance * self._bytes_per_second
            )
            required_size = max(
                self._last_speech_end,
                len(self._audio) - tail_tolerance,
            )

            if self._result and self._result[1] >= required_size:
                target = self._result[0]
            elif self._active and self._active[1] >= required_size:
                target = self._active[0]
            elif self._pending and self._pending[1] >= required_size:
                target = self._pending[0]
            else:
                target = self._request_locked()

            while True:
                if self._result and self._result[0] >= target:
                    sequence, snapshot_size, text = self._result
                    if snapshot_size >= required_size and text.strip():
                        self._closed = True
                        self._condition.notify_all()
                        if self._trace:
                            self._trace.mark("streaming_stt_completed")
                            self._trace.set("streaming_stt_partials", self._partial_count)
                            self._trace.set("streaming_stt_reused", True)
                        return text.strip()
                if self._error_sequence >= target:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(timeout=remaining)

            self._closed = True
            self._condition.notify_all()
            if self._trace:
                self._trace.set("streaming_stt_partials", self._partial_count)
                self._trace.set("streaming_stt_reused", False)
            return None

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._pending is None and not self._closed:
                    self._condition.wait()
                if self._closed:
                    return
                sequence, size, snapshot = self._pending
                self._pending = None
                self._active = (sequence, size)

            try:
                text = (self._transcribe_pcm(snapshot) or "").strip()
            except Exception:
                text = ""

            callback = None
            stable = False
            with self._condition:
                self._active = None
                if self._closed:
                    self._condition.notify_all()
                    return
                if text:
                    normalized = _normalized(text)
                    stable = bool(normalized and normalized == self._previous_normalized)
                    self._previous_normalized = normalized
                    self._partial_count += 1
                    self._result = (sequence, size, text)
                    callback = self._on_partial
                    if self._trace:
                        self._trace.mark("streaming_stt_partial_ready")
                else:
                    self._error_sequence = max(self._error_sequence, sequence)
                self._condition.notify_all()

            if callback:
                try:
                    callback(text, stable)
                except Exception:
                    pass


def _wav_transcriber(sample_rate: int, backend: str) -> Callable[[bytes], str]:
    def _transcribe(pcm: bytes) -> str:
        from .speech import _transcribe_openai_whisper, _transcribe_whisper_cpp

        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        path = tmp.name
        tmp.close()
        try:
            with wave.open(path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                wf.writeframes(pcm)
            if backend == "whisper.cpp":
                return _transcribe_whisper_cpp(path)
            return _transcribe_openai_whisper(path)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    return _transcribe


def create_streaming_transcriber(
    *,
    sample_rate: int = 16000,
    endpoint_silence_secs: float = 0.36,
    on_partial=None,
    trace=None,
):
    """Create rolling STT; auto enables only for the accelerated CLI backend."""
    if STREAMING_MODE in ("0", "false", "off", "disabled"):
        return None
    try:
        from .speech import selected_stt_backend

        backend = selected_stt_backend()
        forced = STREAMING_MODE in ("1", "true", "on", "enabled")
        if backend != "whisper.cpp" and not forced:
            return None
    except Exception:
        return None
    return RollingTranscriber(
        _wav_transcriber(sample_rate, backend),
        sample_rate=sample_rate,
        endpoint_silence_secs=endpoint_silence_secs,
        on_partial=on_partial,
        trace=trace,
    )
