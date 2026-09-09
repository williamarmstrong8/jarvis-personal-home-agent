"""
Two-voice podcast audio generation via Kokoro ONNX (local, free).
Falls back to ElevenLabs speak() per-line if Kokoro models aren't downloaded yet.
"""

import os
import re
import tempfile
import subprocess
import numpy as np

from core.paths import MODELS

GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
RESET  = "\033[0m"

_kokoro = None
_MODEL_PATH  = str(MODELS / "kokoro-v1.0.int8.onnx")
_VOICES_PATH = str(MODELS / "voices.bin")


def _kokoro_available() -> bool:
    return os.path.exists(_MODEL_PATH) and os.path.exists(_VOICES_PATH)


def _get_kokoro():
    global _kokoro
    if _kokoro is None:
        from kokoro_onnx import Kokoro
        _kokoro = Kokoro(_MODEL_PATH, _VOICES_PATH)
    return _kokoro


def parse_script(script: str, host_name: str, cohost_name: str) -> list[tuple[str, str]]:
    """Parse 'Speaker: text' lines into (speaker, text) tuples."""
    lines = []
    for line in script.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(f"{host_name}:"):
            lines.append((host_name, line[len(host_name) + 1:].strip()))
        elif line.startswith(f"{cohost_name}:"):
            lines.append((cohost_name, line[len(cohost_name) + 1:].strip()))
    return lines


def generate_audio_kokoro(
    script: str,
    output_path: str,
    host_name: str,
    cohost_name: str,
    host_voice: str   = "af_sky",
    cohost_voice: str = "am_adam",
    sample_rate: int  = 24000,
) -> str:
    """Generate MP3 using local Kokoro model."""
    import soundfile as sf

    kokoro   = _get_kokoro()
    lines    = parse_script(script, host_name, cohost_name)
    segments = []
    silence  = np.zeros(int(sample_rate * 0.35))

    print(f"{GREEN}[TTS] Generating {len(lines)} lines with Kokoro…{RESET}", flush=True)
    for i, (speaker, text) in enumerate(lines):
        voice = host_voice if speaker == host_name else cohost_voice
        try:
            samples, _sr = kokoro.create(text, voice=voice, speed=1.0, lang="en-us")
            segments.append(samples)
            segments.append(silence)
            print(f"{GREEN}[TTS] {i+1}/{len(lines)} {speaker}: {text[:55]}…{RESET}", flush=True)
        except Exception as exc:
            print(f"{YELLOW}[TTS] Skipped line {i+1}: {exc}{RESET}", flush=True)

    if not segments:
        raise RuntimeError("No audio segments generated.")

    full = np.concatenate(segments)
    wav_path = output_path.replace(".mp3", ".wav")
    sf.write(wav_path, full, sample_rate)

    # Convert WAV → MP3 via ffmpeg (works on Python 3.13+, pydub dropped audioop)
    ffmpeg = next(
        (p for p in ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "ffmpeg"]
         if os.path.exists(p) or p == "ffmpeg"),
        "ffmpeg"
    )
    try:
        result = subprocess.run(
            [ffmpeg, "-y", "-i", wav_path,
             "-codec:a", "libmp3lame", "-b:a", "128k", output_path],
            capture_output=True
        )
        if result.returncode == 0:
            os.remove(wav_path)
            print(f"{GREEN}[TTS] MP3 saved: {output_path}{RESET}", flush=True)
            return output_path
        raise RuntimeError(result.stderr.decode()[:200])
    except Exception as exc:
        print(f"{YELLOW}[TTS] MP3 conversion failed ({exc}) — keeping WAV{RESET}", flush=True)
        final = output_path.replace(".mp3", ".wav")
        if wav_path != final:
            os.rename(wav_path, final)
        return final


def generate_audio_elevenlabs(
    script: str,
    output_path: str,
    host_name: str,
    cohost_name: str,
) -> str:
    """
    Fallback: stitch per-line ElevenLabs audio together into one file.
    Uses the same speak() → afplay approach but writes to temp files and concatenates.
    """
    import wave, struct
    from core.speech import speak as _speak_elevenlabs

    lines    = parse_script(script, host_name, cohost_name)
    tmp_wavs = []

    print(f"{YELLOW}[TTS] Kokoro model not found — using ElevenLabs fallback{RESET}", flush=True)
    print(f"[TTS] Place kokoro-v1.0.int8.onnx + voices.bin in data/models/ to enable local TTS", flush=True)

    for i, (speaker, text) in enumerate(lines):
        # Speak to a temp mp3, convert to wav, collect
        tmp_mp3 = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp_mp3.close()
        try:
            # Write the audio by calling ElevenLabs directly
            import httpx, os
            from dotenv import load_dotenv
            load_dotenv()
            api_key  = os.environ.get("ELEVENLABS_API_KEY", "")
            voice_id = os.environ.get("ELEVENLABS_VOICE_ID", "pNInz6obpgDQGcFmaJgB")
            url      = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
            with httpx.Client(timeout=30) as client:
                with client.stream("POST", url,
                                   headers={"xi-api-key": api_key,
                                            "Content-Type": "application/json"},
                                   json={"text": text, "model_id": "eleven_turbo_v2"}) as resp:
                    resp.raise_for_status()
                    with open(tmp_mp3.name, "wb") as f:
                        for chunk in resp.iter_bytes(4096):
                            f.write(chunk)
            tmp_wavs.append(tmp_mp3.name)
            print(f"[TTS] {i+1}/{len(lines)} {speaker}: {text[:55]}…", flush=True)
        except Exception as exc:
            print(f"{YELLOW}[TTS] Line {i+1} failed: {exc}{RESET}", flush=True)

    if not tmp_wavs:
        raise RuntimeError("No audio generated.")

    # Concatenate all MP3s using afplay-friendly approach with pydub
    try:
        from pydub import AudioSegment
        combined = AudioSegment.empty()
        silence  = AudioSegment.silent(duration=350)
        for mp3 in tmp_wavs:
            combined += AudioSegment.from_mp3(mp3) + silence
        combined.export(output_path, format="mp3", bitrate="128k")
        for mp3 in tmp_wavs:
            try: os.unlink(mp3)
            except: pass
        print(f"{GREEN}[TTS] MP3 saved: {output_path}{RESET}", flush=True)
        return output_path
    except Exception as exc:
        print(f"{RED}[TTS] Concatenation failed: {exc}{RESET}", flush=True)
        # Last resort: just play first segment
        return tmp_wavs[0] if tmp_wavs else output_path


def generate_audio(
    script: str,
    output_path: str,
    host_name: str,
    cohost_name: str,
    host_voice:   str = "af_sky",
    cohost_voice: str = "am_adam",
) -> str:
    """Top-level: use Kokoro if available, else ElevenLabs fallback."""
    if _kokoro_available():
        return generate_audio_kokoro(
            script, output_path, host_name, cohost_name, host_voice, cohost_voice
        )
    else:
        return generate_audio_elevenlabs(script, output_path, host_name, cohost_name)
