#!/usr/bin/env python3
"""
ULTRON — Main entry point: wake word detection + orchestration loop
Uses OpenWakeWord (free, no API key needed) instead of Porcupine.
"""

import argparse
import asyncio
import base64
import collections
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import wave

from core.paths import LOGS, ROOT

# Finder / .app launches get a tiny PATH (no Homebrew). Wake-word doesn't need
# ffmpeg, but other tools do — put brew bins first before anything else runs.
_path = os.environ.get("PATH", "")
for _bin in ("/opt/homebrew/bin", "/usr/local/bin"):
    if _bin not in _path.split(":"):
        _path = f"{_bin}:{_path}"
os.environ["PATH"] = _path

# ── File logger shared by ambient + suit_up ───────────────────────────────────
LOGS.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(LOGS / "jarvis.log"),
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    filemode="a",
)

import numpy as np
import pyaudio
from dotenv import load_dotenv

load_dotenv()

# ── Terminal colours ──────────────────────────────────────────────────────────
CYAN   = "\033[96m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
RESET  = "\033[0m"

def log(msg, colour=CYAN):  print(f"{colour}[ULTRON] {msg}{RESET}", flush=True)
def ok(msg):                log(msg, GREEN)
def warn(msg):              log(msg, YELLOW)
def err(msg):               log(msg, RED)

BANNER = f"""{RED}
██╗   ██╗██╗  ████████╗██████╗  ██████╗ ███╗   ██╗
██║   ██║██║     ██╔══╝██╔══██╗██╔═══██╗████╗  ██║
██║   ██║██║     ██║   ██████╔╝██║   ██║██╔██╗ ██║
██║   ██║██║     ██║   ██╔══██╗██║   ██║██║╚██╗██║
╚██████╔╝███████╗██║   ██║  ██║╚██████╔╝██║ ╚████║
 ╚═════╝ ╚══════╝╚═╝   ╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═══╝
  Peace in our time — v2.0
{RESET}"""

# ── Audio constants ────────────────────────────────────────────────────────────
SAMPLE_RATE        = 16000
CHUNK_SIZE         = 1280          # 80 ms at 16 kHz — required by OpenWakeWord
VAD_SILENCE_SECS   = 1.0          # 1s of silence before we cut the recording
VAD_RMS_THRESHOLD  = 200
MAX_RECORD_SECS    = 10

# Wake word detection threshold (0–1). Lower = more sensitive / more false positives.
WAKE_THRESHOLD = 0.3

# ── Shared async loop + event ─────────────────────────────────────────────────
_async_loop: asyncio.AbstractEventLoop | None = None
_loop_ready = threading.Event()
_running = True


def _request_stop(signum=None, frame=None):
    """SIGTERM/SIGINT — used when the .app quits."""
    global _running
    _running = False


def record_until_silence(stream, seed_frames=(), max_secs: float = MAX_RECORD_SECS) -> bytes:
    """
    Keep reading the already-open mic until silence or max duration.
    seed_frames (preroll) are prepended but not used for VAD, so we don't
    cut off just because the tail of 'Hey Jarvis' went quiet.
    """
    frames = list(seed_frames)
    silent_chunks = 0
    silence_limit = int(VAD_SILENCE_SECS * SAMPLE_RATE / CHUNK_SIZE)
    max_chunks    = int(max_secs * SAMPLE_RATE / CHUNK_SIZE)

    log("Recording…")
    while len(frames) - len(seed_frames) < max_chunks:
        data = stream.read(CHUNK_SIZE, exception_on_overflow=False)
        frames.append(data)
        pcm = np.frombuffer(data, dtype=np.int16)
        rms = np.sqrt(np.mean(pcm.astype(np.float32) ** 2))
        if rms < VAD_RMS_THRESHOLD:
            silent_chunks += 1
            recorded = len(frames) - len(seed_frames)
            if silent_chunks >= silence_limit and recorded > silence_limit:
                break
        else:
            silent_chunks = 0

    return b"".join(frames)


def pcm_to_wav(pcm_bytes: bytes, sample_rate: int = SAMPLE_RATE) -> str:
    """Write raw PCM to a temp WAV file and return its path."""
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    with wave.open(tmp.name, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return tmp.name


def run_async(coro):
    """Submit a coroutine to the shared async loop and block until done."""
    future = asyncio.run_coroutine_threadsafe(coro, _async_loop)
    return future.result()


def fire_async(coro):
    """Schedule a coroutine on the shared loop without waiting."""
    if _async_loop is None:
        return
    asyncio.run_coroutine_threadsafe(coro, _async_loop)


def _stop_wake_mic(wake_stream):
    """Release the input device so TTS doesn't fight CoreAudio AGC/AEC."""
    if wake_stream.is_active():
        try:
            wake_stream.stop_stream()
        except Exception as exc:
            warn(f"Mic stop failed: {exc}")


def _start_wake_mic(wake_stream, oww_model, preroll):
    if not wake_stream.is_active():
        try:
            wake_stream.start_stream()
        except Exception as exc:
            warn(f"Mic start failed: {exc}")
    oww_model.reset()
    preroll.clear()


def _run_command(wake_stream, oww_model, preroll, **kwargs):
    """Stop the mic while STT/brain/TTS run, then resume wake listening."""
    _stop_wake_mic(wake_stream)
    worker = threading.Thread(target=process_command, kwargs=kwargs, daemon=True)
    worker.start()
    worker.join(timeout=180)
    _start_wake_mic(wake_stream, oww_model, preroll)


def _silence_spotify() -> bool:
    """Pause Spotify before the mic records so Whisper doesn't transcribe lyrics."""
    try:
        from core.audio_duck import duck
        return duck()
    except Exception as exc:
        warn(f"Duck failed: {exc}")
        return False


SUIT_UP_TRIGGERS = {
    "suit up", "initialize", "run startup sequence",
    "boot sequence", "power up",
}


def process_command(wav_path: str, is_followup: bool = False,
                    history: list[dict] | None = None):
    """Full pipeline: STT → brain → TTS. duck() is called by the wake loop before this."""
    from core.speech      import transcribe, speak, SpeechQueue, for_speech, release_speaking
    from core.brain       import process_streaming
    from core.ws_server   import broadcast
    from core.context     import get_context_block
    from core.audio_duck  import restore

    t_pipe = time.time()
    transcript = transcribe(wav_path)
    os.unlink(wav_path)

    if not transcript.strip():
        warn("Empty transcript — skipping")
        restore()
        run_async(broadcast({"event": "idle"}))
        from core.brain import generate_in_character
        line = run_async(generate_in_character(
            "The user's speech was empty or unintelligible. "
            "Acknowledge that you didn't catch it.",
            max_tokens=40,
        ))
        if line:
            speak(line)
        return

    log(f"Transcript: {transcript}")
    run_async(broadcast({"event": "transcript", "text": transcript}))

    # ── Suit-up trigger check ─────────────────────────────────────────────────
    normalized = transcript.lower().strip().rstrip(".,!?")
    if normalized in SUIT_UP_TRIGGERS:
        from core.suit_up import run_suit_up_sequence
        log("Suit-up sequence triggered!")
        restore()
        if _ambient:
            _ambient.notify_state("processing")
        run_async(run_suit_up_sequence(broadcast, speak, get_context_block))
        if _ambient:
            _ambient.notify_state("standby")
        return

    # ── Normal pipeline ───────────────────────────────────────────────────────
    if _ambient:
        _ambient.notify_state("processing")

    run_async(broadcast({"event": "thinking"}))

    # Speak each sentence as it streams; prefetch the next while the current plays.
    speech_q = SpeechQueue()
    speech_q.start()
    spoken_parts: list[str] = []
    first_spoken = False
    needs_followup = False
    full_response = ""

    async def on_sentence(text: str):
        nonlocal first_spoken
        cleaned = for_speech(text)
        if not cleaned:
            return
        spoken_parts.append(cleaned)
        if not first_spoken:
            first_spoken = True
            await broadcast({"event": "speaking"})
            if _ambient:
                _ambient.notify_state("speaking")
            log(f"Time-to-speech enqueue {time.time() - t_pipe:.2f}s from pipeline start")
        speech_q.enqueue(cleaned)

    try:
        full_response, needs_followup = run_async(
            process_streaming(transcript, broadcast, on_sentence, history=history)
        )
    finally:
        speech_q.finish()
        release_speaking()

    spoken = " ".join(spoken_parts).strip() or full_response
    if spoken:
        log(f"Spoke: {spoken}")
        run_async(broadcast({"event": "spoke", "text": spoken}))
        if _ambient:
            _ambient.notify_spoke(spoken)

    if needs_followup:
        log("Follow-up expected — reopening mic")
        _followup_event.set()
        # Do NOT restore yet — stays ducked across follow-up turns
    else:
        restore()        # conversation complete — restore volume + Spotify
        run_async(broadcast({"event": "idle"}))
        if _ambient:
            _ambient.notify_state("standby")


_ambient = None   # global AmbientPresence instance

# ── Conversational state ───────────────────────────────────────────────────────
_conversation_history: list[dict] = []   # shared across follow-up turns
_followup_event = threading.Event()      # set by process_command when [FOLLOWUP] detected
MAX_HISTORY_TURNS = 6                    # max messages (3 user+assistant pairs)


def start_async_thread():
    """Start the shared asyncio event loop + ambient presence in a background thread."""
    global _async_loop, _ambient

    def _run():
        global _async_loop, _ambient
        _async_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_async_loop)

        from core.ws_server    import broadcast
        from core.speech       import speak
        from core.context      import get_context_block
        from core.ambient      import AmbientPresence

        _ambient = AmbientPresence()

        async def _boot():
            from core.ws_server import WS_PORT, start_server
            asyncio.ensure_future(
                _ambient.start(broadcast, speak, get_context_block)
            )
            try:
                server = await start_server()
            except OSError as exc:
                err(
                    f"Port {WS_PORT} is already in use — another Jarvis is running. "
                    "Quit the menu-bar app (right-click the dot → Quit Jarvis), "
                    "then start only one copy."
                )
                os._exit(1)
            _loop_ready.set()
            await server.wait_closed()

        _async_loop.run_until_complete(_boot())

    threading.Thread(target=_run, daemon=True).start()
    if not _loop_ready.wait(timeout=10):
        err("WebSocket server failed to start.")
        sys.exit(1)
    ok("WebSocket server ready")


def start_electron():
    """Launch the Electron UI from the project root (dev only)."""
    log("Launching Electron UI…")
    subprocess.Popen(
        ["npx", "electron", "."],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def load_wake_word_model():
    """Load OpenWakeWord model, downloading it first if needed."""
    try:
        import openwakeword
        from openwakeword.model import Model
        import os as _os

        model_path = _os.path.join(
            _os.path.dirname(openwakeword.__file__),
            "resources", "models", "hey_jarvis_v0.1.onnx"
        )

        if not _os.path.exists(model_path):
            log("Downloading OpenWakeWord 'hey_jarvis' model (~5 MB)…")
            # Disable SSL verification for the download (macOS cert issue)
            import ssl
            _orig = ssl._create_default_https_context
            ssl._create_default_https_context = ssl._create_unverified_context
            try:
                openwakeword.utils.download_models(["hey_jarvis"])
            finally:
                ssl._create_default_https_context = _orig

        log("Loading OpenWakeWord model…")
        model = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
        ok("Wake word model ready — say 'Hey Jarvis'")
        return model

    except ImportError:
        err("openwakeword not installed. Run: pip install openwakeword")
        sys.exit(1)
    except Exception as exc:
        err(f"Failed to load wake word model: {exc}")
        sys.exit(1)


# ── CLI test modes ─────────────────────────────────────────────────────────────

def test_spotify():
    from integrations.spotify import play, pause, now_playing
    ok("=== Spotify Test ===")
    print(now_playing())
    print(play("Radiohead", "artist"))
    time.sleep(3)
    print(now_playing())
    print(pause())

def test_gmail():
    from integrations.gmail import draft_email
    ok("=== Gmail Test ===")
    print(draft_email("test@example.com", "Test from Jarvis", "Hello from J.A.R.V.I.S.!"))

def test_notion():
    from integrations.notion import create_page, search
    ok("=== Notion Test ===")
    print(search("test"))
    print(create_page("Jarvis Test Page", "This page was created by J.A.R.V.I.S."))

def test_screen():
    from integrations.screen import capture_screen
    ok("=== Screen Capture Test ===")
    result = capture_screen(force=True)
    if not result.get("ok"):
        err(result.get("error", "Capture failed"))
        return
    out = os.path.join(tempfile.gettempdir(), "ultron-screen-test.jpg")
    with open(out, "wb") as f:
        f.write(base64.b64decode(result["data"]))
    ok(f"{result['width']}x{result['height']}  {result['bytes'] // 1024} KB")
    ok(f"Wrote {out} — open it to confirm Screen Recording is allowed")

def test_pi_mcp():
    from integrations.pi_mcp import anthropic_tools, call_tool, configured
    ok("=== Raspberry Pi MCP Test ===")
    if not configured():
        err("PI_MCP_URL and PI_MCP_TOKEN must be set in .env")
        return
    tools = anthropic_tools()
    if not tools:
        err("Could not list homelab tools — is Tailscale up and pi-mcp running?")
        return
    ok(f"{len(tools)} tools: " + ", ".join(t["name"] for t in tools))
    print(call_tool("pi_get_status", {}))


# ── Main wake-word loop ────────────────────────────────────────────────────────

def main():
    print(BANNER)

    parser = argparse.ArgumentParser(description="ULTRON")
    parser.add_argument("--test-spotify", action="store_true")
    parser.add_argument("--test-gmail",   action="store_true")
    parser.add_argument("--test-notion",  action="store_true")
    parser.add_argument("--test-screen",  action="store_true")
    parser.add_argument("--test-pi-mcp",  action="store_true")
    parser.add_argument(
        "--no-electron",
        action="store_true",
        help="Skip launching Electron (the packaged .app is already the UI)",
    )
    args = parser.parse_args()

    if args.test_spotify: test_spotify(); return
    if args.test_gmail:   test_gmail();   return
    if args.test_notion:  test_notion();  return
    if args.test_screen:  test_screen();  return
    if args.test_pi_mcp:  test_pi_mcp();  return

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    # ── Startup ────────────────────────────────────────────────────────────────
    start_async_thread()
    packaged = args.no_electron or os.environ.get("JARVIS_ELECTRON_PARENT") == "1"
    if not packaged:
        start_electron()

    from core.brain import MODEL as BRAIN_MODEL
    ok(f"Brain model: {BRAIN_MODEL}")

    from core.speech import warmup, is_speaking
    threading.Thread(target=warmup, daemon=True).start()

    # Pre-warm context module — triggers Google OAuth in background before first voice cmd
    def _warmup_context():
        try:
            from core.context import get_context_block
            block = get_context_block()
            ok(f"Context ready")
        except Exception as exc:
            warn(f"Context warmup failed: {exc}")
    threading.Thread(target=_warmup_context, daemon=True).start()

    def _warmup_spotify():
        try:
            from integrations.spotify import is_playing
            is_playing()
        except Exception:
            pass
    threading.Thread(target=_warmup_spotify, daemon=True).start()

    def _warmup_pi_mcp():
        try:
            from integrations.pi_mcp import configured, refresh
            if configured():
                n = len(refresh())
                if n:
                    ok(f"Homelab MCP ready ({n} tools)")
                else:
                    warn("Homelab MCP unreachable — Tailscale or pi-mcp down")
        except Exception as exc:
            warn(f"Homelab MCP warmup failed: {exc}")
    threading.Thread(target=_warmup_pi_mcp, daemon=True).start()

    # ── OpenWakeWord ──────────────────────────────────────────────────────────
    oww_model = load_wake_word_model()

    pa = pyaudio.PyAudio()
    wake_stream = pa.open(
        rate=SAMPLE_RATE,
        channels=1,
        format=pyaudio.paInt16,
        input=True,
        frames_per_buffer=CHUNK_SIZE,
    )

    log(f"Listening for wake word 'Hey Jarvis'… (threshold={WAKE_THRESHOLD})")

    from core.audio_duck import wake_suppressed
    from core.ws_server import broadcast

    # Cooldown: ignore detections for 3s after a trigger to avoid double-firing
    last_triggered = 0.0
    COOLDOWN_SECS  = 3.0
    debug_counter  = 0
    speaking_since = 0.0

    # Pre-roll: last ~1.6s so a run-on "Hey Jarvis play …" keeps the command.
    # 20 chunks × 80ms = 1.6s.
    PREROLL_CHUNKS = 20
    preroll = collections.deque(maxlen=PREROLL_CHUNKS)

    try:
        while _running:
            # Don't capture while TTS is playing — speaker bleed into an open
            # mic is what pops mid-utterance (macOS AGC/echo cancellation).
            if is_speaking():
                if speaking_since == 0.0:
                    speaking_since = time.time()
                elif time.time() - speaking_since > 90:
                    warn("TTS lock stuck — resuming wake word")
                    from core.speech import release_speaking
                    release_speaking()
                    speaking_since = 0.0
                _stop_wake_mic(wake_stream)
                time.sleep(0.04)
                continue
            speaking_since = 0.0
            if not wake_stream.is_active():
                _start_wake_mic(wake_stream, oww_model, preroll)

            # ── Check for pending follow-up BEFORE reading wake word audio ────
            if _followup_event.is_set():
                _followup_event.clear()
                fire_async(broadcast({"event": "followup_listening"}))
                if _ambient:
                    _ambient.notify_state("listening")
                pcm_data = record_until_silence(wake_stream, max_secs=8.0)
                wav_path = pcm_to_wav(pcm_data)
                _run_command(
                    wake_stream, oww_model, preroll,
                    wav_path=wav_path, is_followup=True,
                    history=_conversation_history,
                )
                continue

            raw = wake_stream.read(CHUNK_SIZE, exception_on_overflow=False)
            pcm = np.frombuffer(raw, dtype=np.int16)

            # Keep rolling buffer of recent audio for pre-roll
            preroll.append(raw)

            # OpenWakeWord expects raw int16 PCM at 16kHz
            prediction = oww_model.predict(pcm)

            # Print scores every ~2 seconds so you can see what's happening
            debug_counter += 1
            if debug_counter % 25 == 0:
                score_str = "  ".join(
                    f"{m}: {v:.3f}" for m, v in prediction.items()
                )
                print(f"\r{CYAN}[OWW] {score_str}{RESET}", end="", flush=True)

            # Check scores for all loaded models
            triggered = any(v >= WAKE_THRESHOLD for v in prediction.values())

            now = time.time()
            if triggered and (now - last_triggered) > COOLDOWN_SECS:
                if wake_suppressed():
                    continue
                last_triggered = now
                t_wake = now
                print()  # newline after the score readout
                ok("Wake word detected!")
                oww_model.reset()

                _conversation_history.clear()
                log("Preparing to listen…")

                # Grab preroll NOW — then keep reading this same stream.
                preroll_audio = list(preroll)
                preroll.clear()

                fire_async(broadcast({"event": "wake"}))
                fire_async(broadcast({"event": "listening"}))
                if _ambient:
                    _ambient.notify_state("listening")

                # Pause Spotify *before* recording. Volume-ducking still leaks
                # lyrics into Whisper, and preroll is ~1.6s of that music.
                silenced = _silence_spotify()
                seed = () if silenced else preroll_audio

                log(f"Listening immediately ({(time.time() - t_wake) * 1000:.0f}ms after wake)")
                pcm_data = record_until_silence(wake_stream, seed_frames=seed)
                wav_path = pcm_to_wav(pcm_data)

                _run_command(
                    wake_stream, oww_model, preroll,
                    wav_path=wav_path, history=_conversation_history,
                )

    except KeyboardInterrupt:
        _request_stop()
    finally:
        log("Shutting down…")
        wake_stream.stop_stream()
        wake_stream.close()
        pa.terminate()
        ok("Goodbye, sir.")


if __name__ == "__main__":
    main()
