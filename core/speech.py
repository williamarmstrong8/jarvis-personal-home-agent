"""
J.A.R.V.I.S. — Speech
Whisper STT + Fish Audio TTS (streaming PCM, sentence prefetch)
ElevenLabs remains configured as a fallback.
"""

import os
import queue
import re
import subprocess
import tempfile
import threading
import time
import wave

import httpx
import numpy as np
import sounddevice as sd
import whisper
from dotenv import load_dotenv

load_dotenv()

# ── TTS providers ─────────────────────────────────────────────────────────────
# Default: Fish Audio. Set TTS_PROVIDER=elevenlabs to switch back.
TTS_PROVIDER = os.environ.get("TTS_PROVIDER", "fish").strip().lower()

FISH_AUDIO_API_KEY   = os.environ.get("FISH_AUDIO_API_KEY") or os.environ.get("FISH_API_KEY", "")
FISH_AUDIO_VOICE_ID  = os.environ.get("FISH_AUDIO_MODEL_ID", "")
# s2.1-pro-free is the same engine at $0 (no TTFA/DPA guarantees). Paid: s2.1-pro
FISH_AUDIO_TTS_MODEL = os.environ.get("FISH_AUDIO_TTS_MODEL", "s2.1-pro-free")
FISH_AUDIO_FREE_MODEL = "s2.1-pro-free"

ELEVENLABS_API_KEY  = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "pNInz6obpgDQGcFmaJgB")
ELEVENLABS_MODEL    = os.environ.get("ELEVENLABS_TTS_MODEL", "eleven_flash_v2_5")

PCM_RATE = 24000

CYAN   = "\033[96m"
YELLOW = "\033[93m"
RED    = "\033[91m"
RESET  = "\033[0m"

# ── Whisper (loaded once) ─────────────────────────────────────────────────────
_whisper_model = None


def warmup():
    """Pre-load Whisper model so first transcription is fast."""
    global _whisper_model
    print(f"{CYAN}[SPEECH] Loading Whisper base.en model…{RESET}", flush=True)
    import ssl
    _orig = ssl._create_default_https_context
    ssl._create_default_https_context = ssl._create_unverified_context
    try:
        # English-only is faster than multilingual `base` with no accuracy loss here.
        _whisper_model = whisper.load_model("base.en")
    finally:
        ssl._create_default_https_context = _orig
    _get_tts_client()  # warm keep-alive so the first spoken line isn't a TLS hit
    provider = "Fish Audio" if _use_fish() else ("ElevenLabs" if ELEVENLABS_API_KEY else "pyttsx3")
    print(f"{CYAN}[SPEECH] Whisper ready · TTS={provider}{RESET}", flush=True)


def _get_model():
    global _whisper_model
    if _whisper_model is None:
        warmup()
    return _whisper_model


def _wav_to_float32(wav_path: str) -> np.ndarray:
    """Load a WAV we recorded ourselves — no ffmpeg (Finder-launched apps lack Homebrew PATH)."""
    with wave.open(wav_path, "rb") as wf:
        rate = wf.getframerate()
        ch = wf.getnchannels()
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    if ch > 1:
        pcm = pcm.reshape(-1, ch).mean(axis=1)
    audio = pcm.astype(np.float32) / 32768.0
    if rate != 16000 and len(audio) > 1:
        n_out = int(len(audio) * 16000 / rate)
        x_old = np.linspace(0, 1, len(audio), endpoint=False)
        x_new = np.linspace(0, 1, n_out, endpoint=False)
        audio = np.interp(x_new, x_old, audio).astype(np.float32)
    return audio


def transcribe(wav_path: str) -> str:
    """Transcribe a WAV file with Whisper. Returns the transcript string."""
    try:
        t0     = time.time()
        model  = _get_model()
        result = model.transcribe(
            _wav_to_float32(wav_path),
            language="en",
            fp16=False,
            condition_on_previous_text=False,  # faster on short commands
        )
        text = result.get("text", "").strip()
        print(f"{CYAN}[SPEECH] STT {time.time() - t0:.2f}s{RESET}", flush=True)
        return text
    except Exception as exc:
        print(f"{RED}[SPEECH] Whisper error: {exc}{RESET}", flush=True)
        return ""


# ── Shared TTS plumbing ───────────────────────────────────────────────────────

_tts_client: httpx.Client | None = None
_play_lock = threading.Lock()
_fish_pcm_ok = True
_eleven_pcm_ok = True
_fish_model_override: str | None = None

# Wake-word mic (PyAudio) and TTS (sounddevice) share CoreAudio. Leaving the
# input stream open while speakers play makes macOS AGC/AEC pop mid-utterance.
_speaking_count = 0
_speaking_lock = threading.Lock()

_FADE_SAMPLES = 192          # 8 ms at 24 kHz — kills stream-edge clicks
_PREROLL_BYTES = int(PCM_RATE * 2 * 0.12)  # 120 ms of int16 mono before first write

VOICE_SETTINGS = {
    "stability":         0.4,
    "similarity_boost":  0.85,
    "use_speaker_boost": True,
}


def is_speaking() -> bool:
    """True while TTS is using the speakers — wake loop must release the mic."""
    return _speaking_count > 0


def release_speaking():
    """Drop a stuck speaking lock so the wake loop can listen again."""
    global _speaking_count
    with _speaking_lock:
        _speaking_count = 0


class _SpeakingGuard:
    """Mark playback active and give CoreAudio a beat to drop the input stream."""

    def __enter__(self):
        global _speaking_count
        with _speaking_lock:
            _speaking_count += 1
            first = _speaking_count == 1
        if first:
            time.sleep(0.06)
        return self

    def __exit__(self, *exc):
        global _speaking_count
        with _speaking_lock:
            _speaking_count = max(0, _speaking_count - 1)


def _use_fish() -> bool:
    if TTS_PROVIDER == "elevenlabs":
        return False
    return bool(FISH_AUDIO_API_KEY)


def _get_tts_client() -> httpx.Client:
    global _tts_client
    if _tts_client is None or _tts_client.is_closed:
        _tts_client = httpx.Client(
            timeout=30,
            limits=httpx.Limits(max_keepalive_connections=4, max_connections=8),
        )
    return _tts_client


def _fade_in(samples: np.ndarray, n: int = _FADE_SAMPLES) -> np.ndarray:
    n = min(n, len(samples))
    if n < 2:
        return samples
    out = samples.astype(np.float32, copy=True)
    out[:n] *= np.linspace(0.0, 1.0, n, dtype=np.float32)
    return np.clip(out, -32768, 32767).astype(np.int16)


def _fade_out(samples: np.ndarray, n: int = _FADE_SAMPLES) -> np.ndarray:
    n = min(n, len(samples))
    if n < 2:
        return samples
    out = samples.astype(np.float32, copy=True)
    out[-n:] *= np.linspace(1.0, 0.0, n, dtype=np.float32)
    return np.clip(out, -32768, 32767).astype(np.int16)


class _OutputSession:
    """
    One persistent 24 kHz output stream for a whole reply.
    Reopening PortAudio between sentences is what caused the mid-speech pop.
    """

    def __init__(self):
        self._stream = None
        self._first_write = True

    def __enter__(self):
        self._stream = sd.OutputStream(
            samplerate=PCM_RATE,
            channels=1,
            dtype="int16",
            blocksize=2048,
            latency="high",
        )
        self._stream.start()
        self.write(np.zeros(int(PCM_RATE * 0.02), dtype=np.int16))
        return self

    def write(self, samples: np.ndarray, fade_out: bool = False):
        if samples.size == 0 or self._stream is None:
            return
        audio = np.ascontiguousarray(samples)
        if self._first_write:
            audio = _fade_in(audio)
            self._first_write = False
        if fade_out:
            audio = _fade_out(audio)
        try:
            self._stream.write(audio)
        except Exception as exc:
            print(f"{YELLOW}[SPEECH] Audio write failed: {exc}{RESET}", flush=True)
            self._abort()

    def _abort(self):
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        try:
            stream.abort()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass

    def _drain_and_close(self):
        """Play out what's already queued, then stop. abort() only if stop hangs."""
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        pad_done = threading.Event()

        def _pad():
            try:
                stream.write(np.zeros(int(PCM_RATE * 0.25), dtype=np.int16))
            except Exception:
                pass
            pad_done.set()

        threading.Thread(target=_pad, daemon=True).start()
        pad_done.wait(1.5)
        done = threading.Event()

        def _stop():
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
            done.set()

        threading.Thread(target=_stop, daemon=True).start()
        if not done.wait(4.0):
            print(f"{YELLOW}[SPEECH] Audio stop stalled — aborting leftover buffer{RESET}",
                  flush=True)
            try:
                stream.abort()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass

    def write_bytes(self, pcm: bytes, fade_out: bool = False):
        if len(pcm) % 2:
            pcm = pcm[:-1]
        if not pcm:
            return
        self.write(np.frombuffer(pcm, dtype=np.int16).copy(), fade_out=fade_out)

    def write_chunks(self, chunks, label: str) -> bool:
        leftover = b""
        preroll  = bytearray()
        started  = False
        t0       = time.time()

        def _flush(data: bytes, fade_out: bool = False):
            self.write_bytes(data, fade_out=fade_out)

        for chunk in chunks:
            leftover += chunk
            usable = len(leftover) - (len(leftover) % 2)
            if not usable:
                continue
            piece, leftover = leftover[:usable], leftover[usable:]
            if not started:
                preroll.extend(piece)
                if len(preroll) >= _PREROLL_BYTES:
                    print(
                        f"{CYAN}[SPEECH] TTFA {time.time() - t0:.2f}s ({label}){RESET}",
                        flush=True,
                    )
                    _flush(bytes(preroll))
                    preroll.clear()
                    started = True
            else:
                _flush(piece)

        if preroll:
            if not started:
                print(
                    f"{CYAN}[SPEECH] TTFA {time.time() - t0:.2f}s ({label}){RESET}",
                    flush=True,
                )
            _flush(bytes(preroll))
            started = True
        if leftover and len(leftover) >= 2:
            _flush(leftover[: len(leftover) - (len(leftover) % 2)])
        return started

    def __exit__(self, *exc):
        self._drain_and_close()


def play_pcm(pcm: bytes, rate: int = PCM_RATE, session: _OutputSession | None = None):
    """Play raw 16-bit mono PCM through the default output device."""
    if not pcm:
        return
    if session is not None:
        session.write_bytes(pcm)
        return
    with _SpeakingGuard():
        with _play_lock:
            with _OutputSession() as sess:
                sess.write_bytes(pcm, fade_out=True)


def _play_pcm_chunks(chunks, label: str, session: _OutputSession | None = None) -> bool:
    """Write incoming 16-bit PCM chunks to the speakers. Returns True if audio started."""
    if session is not None:
        return session.write_chunks(chunks, label)
    with _SpeakingGuard():
        with _play_lock:
            with _OutputSession() as sess:
                return sess.write_chunks(chunks, label)


def _play_mp3_bytes(data: bytes) -> bool:
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    try:
        tmp.write(data)
        tmp.flush()
        tmp_path = tmp.name
        tmp.close()
        # Don't deadlock if the PCM session already holds _play_lock.
        acquired = _play_lock.acquire(blocking=False)
        try:
            subprocess.run(["afplay", tmp_path], check=True)
        finally:
            if acquired:
                _play_lock.release()
        return True
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


# ── Fish Audio TTS (primary) ──────────────────────────────────────────────────

def _fish_model() -> str:
    return _fish_model_override or FISH_AUDIO_TTS_MODEL


def _fish_downgrade_to_free() -> bool:
    """On 402, retry once with the free engine instead of failing immediately."""
    global _fish_model_override
    if _fish_model() == FISH_AUDIO_FREE_MODEL:
        return False
    print(
        f"{YELLOW}[SPEECH] Fish Audio needs API credit — switching to {FISH_AUDIO_FREE_MODEL}{RESET}",
        flush=True,
    )
    _fish_model_override = FISH_AUDIO_FREE_MODEL
    return True


def _fish_headers() -> dict:
    return {
        "Authorization": f"Bearer {FISH_AUDIO_API_KEY}",
        "Content-Type":  "application/json",
        "model":         _fish_model(),
    }


def _fish_payload(text: str, fmt: str) -> dict:
    payload = {
        "text":         text,
        "format":       fmt,
        "latency":      "balanced",
        "chunk_length": 150,
        "normalize":    True,
    }
    if fmt == "pcm":
        payload["sample_rate"] = PCM_RATE
    if FISH_AUDIO_VOICE_ID:
        payload["reference_id"] = FISH_AUDIO_VOICE_ID
    return payload


def _fish_stream(text: str, fmt: str):
    return _get_tts_client().stream(
        "POST",
        "https://api.fish.audio/v1/tts",
        headers=_fish_headers(),
        json=_fish_payload(text, fmt),
    )


def _fish_synthesize_pcm(text: str) -> bytes | None:
    if not text.strip() or not FISH_AUDIO_API_KEY:
        return None
    try:
        while True:
            with _fish_stream(text, "pcm") as resp:
                if resp.status_code in (402, 429):
                    if resp.status_code == 402 and _fish_downgrade_to_free():
                        continue
                    print(f"{YELLOW}[SPEECH] Fish Audio quota exceeded{RESET}", flush=True)
                    return None
                resp.raise_for_status()
                return b"".join(resp.iter_bytes(chunk_size=2048))
    except Exception as exc:
        print(f"{RED}[SPEECH] Fish synthesize error: {exc}{RESET}", flush=True)
        return None


def _fish_speak_streaming(text: str, session: _OutputSession | None = None) -> bool:
    """Stream Fish PCM to the speakers. Returns True if speech started."""
    global _fish_pcm_ok
    if _fish_pcm_ok:
        started = False
        try:
            while True:
                with _fish_stream(text, "pcm") as resp:
                    if resp.status_code in (402, 429):
                        if resp.status_code == 402 and _fish_downgrade_to_free():
                            continue
                        print(f"{YELLOW}[SPEECH] Fish Audio quota exceeded{RESET}", flush=True)
                        return False
                    resp.raise_for_status()
                    started = _play_pcm_chunks(
                        resp.iter_bytes(chunk_size=1024), "fish", session=session
                    )
                    return True
        except Exception as exc:
            if started:
                return True
            print(f"{YELLOW}[SPEECH] Fish PCM stream failed ({exc}) — trying MP3{RESET}",
                  flush=True)
            _fish_pcm_ok = False

    return _fish_speak_mp3(text)


def _fish_speak_mp3(text: str) -> bool:
    try:
        while True:
            with _fish_stream(text, "mp3") as resp:
                if resp.status_code in (402, 429):
                    if resp.status_code == 402 and _fish_downgrade_to_free():
                        continue
                    return False
                resp.raise_for_status()
                data = b"".join(resp.iter_bytes(chunk_size=2048))
            return _play_mp3_bytes(data)
    except Exception as exc:
        print(f"{RED}[SPEECH] Fish MP3 playback failed: {exc}{RESET}", flush=True)
        return False


# ── ElevenLabs TTS (kept as fallback) ─────────────────────────────────────────

def _eleven_request(text: str):
    """Open a streaming PCM response from ElevenLabs."""
    url = (
        f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}/stream"
        f"?optimize_streaming_latency=4&output_format=pcm_{PCM_RATE}"
    )
    headers = {
        "xi-api-key":   ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
        "Accept":       "application/octet-stream",
    }
    payload = {
        "text":           text,
        "model_id":       ELEVENLABS_MODEL,
        "voice_settings": VOICE_SETTINGS,
    }
    return _get_tts_client().stream("POST", url, headers=headers, json=payload)


def _eleven_synthesize_pcm(text: str) -> bytes | None:
    if not text.strip() or not ELEVENLABS_API_KEY:
        return None
    try:
        with _eleven_request(text) as resp:
            if resp.status_code == 429:
                print(f"{YELLOW}[SPEECH] ElevenLabs quota exceeded{RESET}", flush=True)
                return None
            resp.raise_for_status()
            return b"".join(resp.iter_bytes(chunk_size=2048))
    except Exception as exc:
        print(f"{RED}[SPEECH] ElevenLabs synthesize error: {exc}{RESET}", flush=True)
        return None


def _eleven_speak_mp3(text: str) -> bool:
    """Download MP3 and play with afplay. Returns True on success."""
    try:
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}/stream"
        headers = {
            "xi-api-key":   ELEVENLABS_API_KEY,
            "Content-Type": "application/json",
        }
        payload = {
            "text":           text,
            "model_id":       ELEVENLABS_MODEL,
            "voice_settings": VOICE_SETTINGS,
        }
        with _get_tts_client().stream("POST", url, headers=headers, json=payload) as resp:
            if resp.status_code == 429:
                return False
            resp.raise_for_status()
            data = b"".join(resp.iter_bytes(chunk_size=2048))
        return _play_mp3_bytes(data)
    except Exception as exc:
        print(f"{RED}[SPEECH] ElevenLabs MP3 playback failed: {exc}{RESET}", flush=True)
        return False


def _eleven_speak_streaming(text: str, session: _OutputSession | None = None) -> bool:
    global _eleven_pcm_ok
    if not ELEVENLABS_API_KEY:
        return False

    if _eleven_pcm_ok:
        started = False
        try:
            with _eleven_request(text) as resp:
                if resp.status_code == 429:
                    print(f"{YELLOW}[SPEECH] ElevenLabs quota exceeded{RESET}", flush=True)
                    return False
                resp.raise_for_status()
                started = _play_pcm_chunks(
                    resp.iter_bytes(chunk_size=1024), "eleven", session=session
                )
                return True
        except Exception as exc:
            if started:
                return True
            print(f"{YELLOW}[SPEECH] ElevenLabs PCM stream failed ({exc}) — using MP3{RESET}",
                  flush=True)
            _eleven_pcm_ok = False

    return _eleven_speak_mp3(text)


# ── Public TTS API ────────────────────────────────────────────────────────────

def for_speech(text: str) -> str:
    """Strip markup TTS would read aloud (*asterisks*, **bold**, `code`, links)."""
    t = text.replace("[FOLLOWUP]", "")
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)
    t = re.sub(r"__(.+?)__", r"\1", t)
    t = re.sub(r"\*(.+?)\*", r"\1", t)
    t = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", t)
    t = t.replace("*", "").replace("`", "")
    t = re.sub(r" {2,}", " ", t)
    return t.strip()


def synthesize_pcm(text: str) -> bytes | None:
    """Fetch a complete PCM buffer. Returns None on failure (caller may fall back)."""
    text = for_speech(text)
    if not text:
        return None
    if _use_fish():
        pcm = _fish_synthesize_pcm(text)
        if pcm:
            return pcm
    return _eleven_synthesize_pcm(text)


def speak_streaming(text: str, session: _OutputSession | None = None):
    """Stream TTS to the speakers as bytes arrive — lowest time-to-first-audio."""
    text = for_speech(text)
    if not text:
        return

    if _use_fish():
        if _fish_speak_streaming(text, session=session):
            return
        if ELEVENLABS_API_KEY:
            print(f"{YELLOW}[SPEECH] Fish Audio failed — falling back to ElevenLabs{RESET}",
                  flush=True)

    if _eleven_speak_streaming(text, session=session):
        return

    print(f"{YELLOW}[SPEECH] Cloud TTS unavailable — falling back to pyttsx3{RESET}",
          flush=True)
    _speak_fallback(text)


def speak(text: str):
    """Speak a single utterance (ambient, errors, one-off lines)."""
    speak_streaming(text)


def _speak_mp3(text: str) -> bool:
    """MP3 fallback used by SpeechQueue when a prefetched PCM buffer is missing."""
    text = for_speech(text)
    if not text:
        return False
    if _use_fish() and _fish_speak_mp3(text):
        return True
    return _eleven_speak_mp3(text)


def _speak_fallback(text: str):
    """Last-resort TTS using pyttsx3 when cloud voices are unavailable."""
    text = for_speech(text)
    if not text:
        return
    try:
        import pyttsx3
        engine = pyttsx3.init()
        engine.setProperty("rate", 175)
        engine.say(text)
        engine.runAndWait()
    except Exception as exc:
        print(f"{RED}[SPEECH] Fallback TTS failed: {exc}{RESET}", flush=True)


# ── Sentence queue: first sentence streams immediately, later ones prefetch ───

class SpeechQueue:
    """
    Speak sentences as they arrive from the model.

    First sentence streams to the speakers the moment it is enqueued.
    Later sentences synthesize in the background while the current one plays,
    so gaps between sentences stay near zero.
    """

    def __init__(self):
        self._texts: queue.Queue[str | None] = queue.Queue()
        self._thread: threading.Thread | None = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def enqueue(self, text: str):
        cleaned = for_speech(text)
        if cleaned:
            self._texts.put(cleaned)

    def finish(self, timeout: float = 120.0):
        self._texts.put(None)
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self):
        first = self._texts.get()
        if first is None:
            return

        pcm_q: queue.Queue[tuple[str, bytes | None] | None] = queue.Queue(maxsize=2)

        def synthesizer():
            while True:
                text = self._texts.get()
                if text is None:
                    pcm_q.put(None)
                    return
                pcm_q.put((text, synthesize_pcm(text)))

        threading.Thread(target=synthesizer, daemon=True).start()

        try:
            with _SpeakingGuard():
                with _play_lock:
                    with _OutputSession() as session:
                        speak_streaming(first, session=session)
                        while True:
                            try:
                                item = pcm_q.get(timeout=0.04)
                            except queue.Empty:
                                # Don't write silence into a possibly-stalled
                                # PortAudio stream (hangs when Spotify takes over).
                                continue
                            if item is None:
                                break
                            text, pcm = item
                            if pcm:
                                session.write_bytes(pcm)
                            elif not _speak_mp3(text):
                                _speak_fallback(text)
        except Exception as exc:
            print(f"{RED}[SPEECH] Queue error: {exc}{RESET}", flush=True)
        finally:
            release_speaking()
