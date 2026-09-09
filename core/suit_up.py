"""
J.A.R.V.I.S. — Suit Up Sequence
Cinematic startup ritual. Triggered by voice, fully async and cancellable.

Sound files: place in assets/sounds/
  power_hum.wav     → freesound.org search: "electrical hum startup"
  reactor_online.wav → freesound.org search: "arc reactor hum power on"
"""

import asyncio
import logging
import os
import re
import subprocess
from typing import Callable, Awaitable

from .paths import ASSETS

log = logging.getLogger("jarvis.suit_up")

SOUNDS_DIR = str(ASSETS / "sounds")

SYSTEM_CHECKS = [
    "NEURAL INTERFACE",
    "VOICE PIPELINE",
    "WHISPER STT",
    "CLAUDE BRAIN",
    "FISH AUDIO TTS",
    "SPOTIFY",
    "GMAIL",
    "NOTION",
    "SITUATIONAL AWARENESS",
]


def _play_sound(filename: str):
    """Play a sound file non-blocking via afplay. Skip silently if missing."""
    path = os.path.join(SOUNDS_DIR, filename)
    if not os.path.exists(path):
        log.info("Sound file not found, skipping: %s", path)
        return
    try:
        subprocess.Popen(
            ["afplay", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        log.warning("Could not play sound %s: %s", filename, exc)


def _briefing_facts(context_block: str) -> str:
    """Pull notable facts from the context block. No spoken phrasing."""

    def _field(label: str) -> str:
        m = re.search(rf"^{label}:\s*(.+)$", context_block, re.MULTILINE)
        return m.group(1).strip() if m else ""

    time_str   = _field("TIME")
    weather    = _field("WEATHER")
    next_event = _field("NEXT EVENT")
    unread     = _field("UNREAD EMAILS")

    facts = []
    if time_str:
        facts.append(f"TIME: {time_str}")

    if weather and "unavailable" not in weather:
        notable = any(w in weather.lower()
                      for w in ["rain", "storm", "snow", "thunder", "drizzle", "fog"])
        try:
            temp_c = int(re.search(r"(-?\d+)°C", weather).group(1))
            extreme = temp_c < 5 or temp_c > 30
        except Exception:
            extreme = False
        if notable or extreme:
            facts.append(f"WEATHER: {weather}")

    if next_event and next_event != "none today":
        m = re.search(r"in (\d+) minutes", next_event)
        if m and int(m.group(1)) <= 120:
            facts.append(f"NEXT EVENT: {next_event}")

    try:
        count = int(unread)
        if count > 0:
            facts.append(f"UNREAD EMAILS: {count}")
    except (ValueError, TypeError):
        pass

    return "\n".join(facts)


async def run_suit_up_sequence(
    broadcast:  Callable[[dict], Awaitable[None]],
    speak_fn:   Callable[[str], None],
    context_fn: Callable[[], str],
):
    """
    Execute the full J.A.R.V.I.S. suit-up startup sequence.
    Fully async — raises CancelledError cleanly if interrupted.
    """
    loop = asyncio.get_event_loop()
    try:
        log.info("Suit-up sequence initiated")

        # Step 1 — Blackout
        await broadcast({"type": "suit_up_start"})

        # Step 2 — Power hum (0.3s)
        await asyncio.sleep(0.3)
        await broadcast({"type": "suit_up_phase",
                         "phase": "power", "label": "POWER SYSTEMS ONLINE"})
        _play_sound("power_hum.wav")

        # Step 3 — Systems check (1.2s)
        await asyncio.sleep(0.9)
        await broadcast({"type": "suit_up_phase",
                         "phase": "systems", "label": "RUNNING DIAGNOSTICS"})
        await asyncio.sleep(0.8)

        for item in SYSTEM_CHECKS:
            await broadcast({"type": "suit_up_check", "item": item, "status": "OK"})
            await asyncio.sleep(0.18)

        # Step 4 — Reactor online (~3.5s cumulative)
        await asyncio.sleep(0.32)
        await broadcast({"type": "suit_up_phase",
                         "phase": "reactor", "label": "ARC REACTOR STABLE"})
        _play_sound("reactor_online.wav")

        # Step 5 — Context assembly (~4.2s)
        await asyncio.sleep(0.7)
        await broadcast({"type": "suit_up_phase",
                         "phase": "context", "label": "LOADING ENVIRONMENT"})
        context_block = await loop.run_in_executor(None, context_fn)

        # Step 6 — Spoken briefing (~5.0s)
        await asyncio.sleep(0.8)
        from core.brain import generate_in_character
        facts = _briefing_facts(context_block)
        briefing = await generate_in_character(
            "You just came online. Give the startup briefing from these facts.\n\n"
            + (facts or "No extra context."),
            max_tokens=120,
        )
        if briefing:
            log.info("Suit-up briefing: %s", briefing)
            await loop.run_in_executor(None, speak_fn, briefing)
        else:
            log.warning("Suit-up briefing skipped — model unavailable")

        # Step 7 — Complete
        await broadcast({"type": "suit_up_complete"})
        log.info("Suit-up sequence complete")

    except asyncio.CancelledError:
        log.info("Suit-up sequence cancelled by pipeline")
        try:
            await broadcast({"type": "suit_up_complete"})
        except Exception:
            pass
        raise
    except Exception as exc:
        log.warning("Suit-up sequence error: %s", exc)
        try:
            await broadcast({"type": "suit_up_complete"})
        except Exception:
            pass
