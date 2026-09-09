"""
ULTRON — Screen capture
Grab the macOS display (cursor included) for Claude vision.
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
import tempfile
import threading
import time

GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
RESET  = "\033[0m"

# Anthropic's efficient vision size is ~1568px on the long edge.
# 1280 keeps tokens + latency down for a spoken 1-sentence reply.
MAX_EDGE = 1280
JPEG_QUALITY = 70
CACHE_TTL_SECS = 15.0

PERMISSION_HELP = (
    "Screen Recording is blocked. System Settings → Privacy & Security → "
    "Screen Recording — enable it for Jarvis (or Terminal / Cursor if you "
    "launched from there), then restart Jarvis."
)

# Only attach a screenshot when the user asked about what is ON the screen.
_SCREEN_HINTS = re.compile(
    r"(?i)\b("
    r"(?:look(?:ing)? at|see|check|what's on|what is on|whats on|what am i looking at)"
    r" (?:my |the )?(?:screen|display|monitor|desktop)|"
    r"(?:can you |could you )?(?:see|look at) (?:my |the )?(?:screen|display|monitor|desktop)|"
    r"what(?:'s| is|s) on (?:my |the )?(?:screen|display|monitor|desktop)|"
    r"what (?:do you |can you )?see on (?:my |the )?(?:screen|display|monitor|desktop)|"
    r"(?:take a |grab a |capture (?:my |the )?)?screenshot|"
    r"describe (?:my |the )?(?:screen|display|what you see)"
    r")\b"
)

_lock = threading.Lock()
_cache: dict | None = None
_cache_ts = 0.0


def looks_like_screen_question(transcript: str) -> bool:
    return bool(_SCREEN_HINTS.search(transcript or ""))


def prefetch() -> None:
    """Capture now so a later look_at_screen call is instant."""
    capture_screen(force=True)


def _sips_dim(path: str, key: str) -> int:
    r = subprocess.run(
        ["sips", "-g", key, path],
        capture_output=True, text=True, timeout=5,
    )
    for line in r.stdout.splitlines():
        if key in line:
            try:
                return int(line.split(":")[-1].strip())
            except ValueError:
                return 0
    return 0


def capture_screen(*, force: bool = False) -> dict:
    """
    Capture the display as a JPEG and return a dict:
      ok=True  → media_type, data (base64), text, width, height
      ok=False → error
    """
    global _cache, _cache_ts

    with _lock:
        if (
            not force
            and _cache
            and _cache.get("ok")
            and (time.time() - _cache_ts) < CACHE_TTL_SECS
        ):
            return _cache

    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    path = tmp.name
    tmp.close()

    try:
        proc = subprocess.run(
            ["screencapture", "-x", "-C", "-t", "jpg", path],
            capture_output=True, text=True, timeout=8,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "screencapture failed").strip()
            result = {"ok": False, "error": f"{PERMISSION_HELP} ({err})"}
            print(f"{RED}[SCREEN] Capture failed: {err}{RESET}", flush=True)
            return result

        if not os.path.exists(path) or os.path.getsize(path) < 4000:
            print(f"{YELLOW}[SCREEN] Capture empty/tiny — likely no Screen Recording permission{RESET}", flush=True)
            return {"ok": False, "error": PERMISSION_HELP}

        subprocess.run(
            ["sips", "-Z", str(MAX_EDGE),
             "-s", "format", "jpeg",
             "-s", "formatOptions", str(JPEG_QUALITY),
             path],
            capture_output=True, timeout=8,
        )

        size = os.path.getsize(path)
        if size < 4000:
            return {"ok": False, "error": PERMISSION_HELP}

        width  = _sips_dim(path, "pixelWidth")
        height = _sips_dim(path, "pixelHeight")
        with open(path, "rb") as f:
            data = base64.b64encode(f.read()).decode("ascii")

        result = {
            "ok": True,
            "media_type": "image/jpeg",
            "data": data,
            "width": width,
            "height": height,
            "bytes": size,
            "text": (
                f"Screenshot of the user's macOS display, captured just now "
                f"({width}x{height}, cursor included)."
            ),
        }
        print(
            f"{GREEN}[SCREEN] Captured {width}x{height} ({size // 1024} KB){RESET}",
            flush=True,
        )
        with _lock:
            _cache = result
            _cache_ts = time.time()
        return result

    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Screen capture timed out."}
    except Exception as exc:
        print(f"{RED}[SCREEN] {exc}{RESET}", flush=True)
        return {"ok": False, "error": f"Screen capture failed: {exc}"}
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def image_content_blocks(shot: dict) -> list[dict]:
    """Anthropic content blocks for a successful capture."""
    return [
        {"type": "text", "text": shot.get("text", "Screenshot captured.")},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": shot.get("media_type", "image/jpeg"),
                "data": shot["data"],
            },
        },
    ]
