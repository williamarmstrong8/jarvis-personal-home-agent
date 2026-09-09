"""
J.A.R.V.I.S. — Ambient Presence
Background life: idle remarks, breathing pulses, time-aware events, alerts.
Runs as a background asyncio task. Never blocks the voice pipeline.
"""

import asyncio
import logging
import random
import re
import time
from datetime import datetime
from typing import Callable, Awaitable

# ── Logging (file only) ───────────────────────────────────────────────────────
log = logging.getLogger("jarvis.ambient")

# Ambient remarks use the same cheap model as the voice brain.

SUIT_UP_TRIGGERS = {
    "suit up", "initialize", "run startup sequence",
    "boot sequence", "power up",
}


class AmbientPresence:
    def __init__(self):
        self._state               = "standby"
        self._task: asyncio.Task  = None
        self._last_spoke_at       = 0.0
        self._last_remark_at      = 0.0
        self._last_battery_alert  = 0.0
        self._last_weather_str    = None
        self._fired_today: set    = set()
        self._fired_date          = None
        self._broadcast           = None
        self._speak_fn            = None
        self._context_fn          = None

    # ── Public interface ──────────────────────────────────────────────────────

    async def start(
        self,
        broadcast:   Callable[[dict], Awaitable[None]],
        speak_fn:    Callable[[str], None],
        context_fn:  Callable[[], str],
    ):
        """Begin the ambient background loop. Runs until cancelled."""
        self._broadcast  = broadcast
        self._speak_fn   = speak_fn
        self._context_fn = context_fn
        self._task       = asyncio.current_task()
        log.info("Ambient presence started")
        try:
            await self._loop()
        except asyncio.CancelledError:
            log.info("Ambient presence stopped")

    def stop(self):
        """Cancel the background task cleanly."""
        if self._task and not self._task.done():
            self._task.cancel()

    def notify_state(self, state: str):
        """Call from any thread when pipeline state changes."""
        self._state = state
        log.info("Ambient state → %s", state)

    def notify_spoke(self, text: str):
        """Call after every Jarvis utterance to reset ambient cooldowns."""
        self._last_spoke_at = time.time()

    # ── Internal helpers ──────────────────────────────────────────────────────

    @property
    def _is_standby(self) -> bool:
        return self._state == "standby"

    def _mins_since_spoke(self) -> float:
        return (time.time() - self._last_spoke_at) / 60.0

    async def _get_context(self) -> str:
        loop = asyncio.get_event_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, self._context_fn),
                timeout=4.0,
            )
        except Exception as exc:
            log.warning("Context fetch failed: %s", exc)
            return ""

    async def _claude_mini(self, prompt: str, max_tokens: int = 50) -> str | None:
        """Make a small LLM call using the Jarvis system prompt."""
        from core.brain import generate_in_character
        return await generate_in_character(prompt, max_tokens=max_tokens)

    async def _say(self, text: str):
        """Broadcast ambient_remark signal, speak, then update cooldown."""
        if not self._is_standby:
            return
        try:
            await self._broadcast({"type": "ambient_remark"})
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._speak_fn, text)
            self.notify_spoke(text)
            log.info("Ambient spoke: %s", text)
        except Exception as exc:
            log.warning("Ambient say error: %s", exc)

    # ── Daily event tracking ──────────────────────────────────────────────────

    def _reset_daily_if_needed(self):
        today = datetime.now().date()
        if self._fired_date != today:
            self._fired_today = set()
            self._fired_date  = today
            log.info("Ambient daily state reset")

    def _already_fired(self, name: str) -> bool:
        self._reset_daily_if_needed()
        return name in self._fired_today

    def _mark_fired(self, name: str):
        self._reset_daily_if_needed()
        self._fired_today.add(name)

    # ── Behavior 1: Idle remarks ──────────────────────────────────────────────

    async def _maybe_idle_remark(self):
        """Generate a short dry remark via Claude when idle long enough."""
        if not self._is_standby:
            return
        if self._mins_since_spoke() < 3:
            return
        mins_since_remark = (time.time() - self._last_remark_at) / 60.0
        if mins_since_remark < 8:
            return

        context = await self._get_context()
        prompt  = (
            "Generate a single short ambient remark (max 12 words) "
            "as if thinking out loud while idle. No question. "
            "Return only the remark.\n"
            f"Current context:\n{context}"
        )
        remark = await self._claude_mini(prompt, max_tokens=40)
        if remark and self._is_standby:
            await self._say(remark)
            self._last_remark_at = time.time()

    # ── Behavior 2: Ambient pulse ─────────────────────────────────────────────

    async def _ambient_pulse(self):
        """Send a silent heartbeat pulse to the globe."""
        if not self._is_standby:
            return
        try:
            intensity = round(random.uniform(0.3, 0.8), 2)
            await self._broadcast({"type": "ambient_pulse", "intensity": intensity})
            log.info("Ambient pulse %.2f", intensity)
        except Exception as exc:
            log.warning("Ambient pulse error: %s", exc)

    # ── Behavior 3: Time-aware micro-events ───────────────────────────────────

    async def _check_time_events(self):
        if not self._is_standby:
            return

        now       = datetime.now()
        hour      = now.hour
        is_weekday = now.weekday() < 5

        # 9 AM — morning brief
        if hour == 9 and is_weekday and not self._already_fired("morning_brief"):
            self._mark_fired("morning_brief")
            try:
                loop = asyncio.get_event_loop()
                events_str = await loop.run_in_executor(
                    None, _list_today_events
                )
                remark = await self._claude_mini(
                    "Give a brief morning briefing.\n"
                    f"Today's calendar:\n{events_str}",
                    max_tokens=80,
                )
                if remark:
                    await self._say(remark)
            except Exception as exc:
                log.warning("Morning brief failed: %s", exc)

        # 1 PM — afternoon check
        elif hour == 13 and is_weekday and not self._already_fired("afternoon_check"):
            self._mark_fired("afternoon_check")
            try:
                context = await self._get_context()
                m = re.search(r"NEXT EVENT:\s*(.+)", context)
                if m and m.group(1).strip() != "none today":
                    remark = await self._claude_mini(
                        "It is afternoon. Give a short remark about the remaining schedule "
                        "if anything still matters.\n"
                        f"Context:\n{context}",
                        max_tokens=40,
                    )
                    if remark:
                        await self._say(remark)
            except Exception as exc:
                log.warning("Afternoon check failed: %s", exc)

        # 6 PM — end of day
        elif hour == 18 and is_weekday and not self._already_fired("eod_remark"):
            self._mark_fired("eod_remark")
            try:
                context = await self._get_context()
                remark  = await self._claude_mini(
                    "It is 6 PM and the workday is ending. One short remark.\n"
                    f"Context:\n{context}",
                    max_tokens=40,
                )
                if remark:
                    await self._say(remark)
            except Exception as exc:
                log.warning("EOD remark failed: %s", exc)

        # Midnight — reset daily state
        elif hour == 0 and not self._already_fired("midnight_reset"):
            self._mark_fired("midnight_reset")
            self._fired_today = {"midnight_reset"}  # keep only the reset marker
            log.info("Midnight: daily ambient state reset")

    # ── Behavior 4: Low battery alert ────────────────────────────────────────

    async def _check_battery(self):
        if not self._is_standby:
            return
        mins_since_alert = (time.time() - self._last_battery_alert) / 60.0
        if mins_since_alert < 60:
            return
        try:
            from .context import get_battery
            loop    = asyncio.get_event_loop()
            battery = await loop.run_in_executor(None, get_battery)
            if battery is not None and battery < 20:
                self._last_battery_alert = time.time()
                remark = await self._claude_mini(
                    f"Battery is at {battery} percent. Give a short low-battery alert.",
                    max_tokens=40,
                )
                if remark:
                    await self._say(remark)
        except Exception as exc:
            log.warning("Battery check failed: %s", exc)

    # ── Behavior 5: Weather change detection ─────────────────────────────────

    async def _check_weather_change(self):
        if not self._is_standby:
            return
        try:
            context = await self._get_context()
            m = re.search(r"WEATHER:\s*(.+)", context)
            if not m:
                return
            current = m.group(1).strip()

            if self._last_weather_str is None:
                self._last_weather_str = current
                return

            # Compare key descriptor words
            descriptors = {
                "rain", "sunny", "cloudy", "cloud", "storm", "snow",
                "clear", "fog", "drizzle", "thunder", "overcast",
            }
            old_desc = {w for w in self._last_weather_str.lower().split()
                        if w in descriptors}
            new_desc = {w for w in current.lower().split() if w in descriptors}

            if old_desc != new_desc:
                self._last_weather_str = current
                remark = await self._claude_mini(
                    f"The weather changed. Current conditions: {current}. One short remark.",
                    max_tokens=40,
                )
                if remark:
                    await self._say(remark)
        except Exception as exc:
            log.warning("Weather change check failed: %s", exc)

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def _loop(self):
        """Central timer loop — ticks every 5 seconds."""
        now_ts = time.time()

        # Stagger intervals so they don't all fire at once on startup
        last_pulse       = now_ts
        last_remark      = now_ts
        last_battery     = now_ts
        last_weather     = now_ts
        last_time_check  = now_ts

        pulse_interval   = random.uniform(45, 90)
        remark_interval  = random.uniform(8 * 60, 20 * 60)

        while True:
            try:
                await asyncio.sleep(5)
                now = time.time()

                if now - last_pulse >= pulse_interval:
                    await self._ambient_pulse()
                    last_pulse     = now
                    pulse_interval = random.uniform(45, 90)

                if now - last_remark >= remark_interval:
                    await self._maybe_idle_remark()
                    last_remark     = now
                    remark_interval = random.uniform(8 * 60, 20 * 60)

                if now - last_battery >= 5 * 60:
                    await self._check_battery()
                    last_battery = now

                if now - last_weather >= 15 * 60:
                    await self._check_weather_change()
                    last_weather = now

                if now - last_time_check >= 60:
                    await self._check_time_events()
                    last_time_check = now

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Ambient loop error: %s", exc)
                await asyncio.sleep(10)


# ── Helper (imported lazily to avoid circular imports) ────────────────────────

def _list_today_events() -> str:
    try:
        from integrations.calendar import list_events
        result = list_events("today")
        # Truncate for voice
        return result[:300]
    except Exception as exc:
        log.warning("Morning brief calendar fetch failed: %s", exc)
        return "Calendar unavailable."
