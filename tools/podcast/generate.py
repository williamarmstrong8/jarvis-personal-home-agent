"""
J.A.R.V.I.S. — Daily Podcast Tool
Orchestrates: data gathering → Claude script → Kokoro TTS → macOS notification.
"""

import json
import threading
from datetime import datetime
from core.paths import CONFIG, PODCASTS

GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
RESET  = "\033[0m"

# ── Config ─────────────────────────────────────────────────────────────────────

_CONFIG_PATH = CONFIG / "podcast_config.json"


def _load_config() -> dict:
    with open(_CONFIG_PATH) as f:
        return json.load(f)


# ── Data helpers ───────────────────────────────────────────────────────────────

def _get_calendar_summary() -> str:
    """Pull today's calendar events into a plain-text summary."""
    try:
        from integrations.calendar import list_events
        return list_events("today")
    except Exception as exc:
        return f"Calendar unavailable: {exc}"


def _get_email_summary() -> str:
    """Pull recent unread emails into a plain-text summary."""
    try:
        from integrations.gmail import search_emails
        return search_emails("is:unread", max_results=8)
    except Exception as exc:
        return f"Email unavailable: {exc}"


# ── Main pipeline ──────────────────────────────────────────────────────────────

def generate_daily_podcast() -> str:
    """
    Full pipeline:
      1. Fetch weather, news, emails, calendar
      2. Claude writes two-host script
      3. Kokoro TTS generates audio
      4. macOS notification fires with Play button
    Returns a JARVIS-speakable confirmation string.
    """
    try:
        config = _load_config()
        loc    = config["location"]
        pc     = config["podcast"]

        print(f"{GREEN}[PODCAST] Starting daily podcast generation…{RESET}", flush=True)

        # ── 1. Data gathering ─────────────────────────────────────────────────
        print(f"{GREEN}[PODCAST] Fetching weather…{RESET}", flush=True)
        from tools.data_sources.weather import get_weather
        weather = get_weather(loc["latitude"], loc["longitude"], loc["city"])

        print(f"{GREEN}[PODCAST] Fetching news…{RESET}", flush=True)
        from tools.data_sources.news import get_news, format_news_for_prompt
        articles  = get_news(config["news_sources"], config["interests"])
        news_text = format_news_for_prompt(articles)

        print(f"{GREEN}[PODCAST] Fetching emails…{RESET}", flush=True)
        email_summary = _get_email_summary()

        print(f"{GREEN}[PODCAST] Fetching calendar…{RESET}", flush=True)
        calendar_summary = _get_calendar_summary()

        # ── 2. Script generation ──────────────────────────────────────────────
        print(f"{GREEN}[PODCAST] Writing script with Claude…{RESET}", flush=True)
        from tools.podcast.script import generate_podcast_script
        script = generate_podcast_script(
            weather          = weather,
            news_text        = news_text,
            email_summary    = email_summary,
            calendar_summary = calendar_summary,
            interests        = config["interests"],
            host_name        = pc["host_name"],
            cohost_name      = pc["cohost_name"],
            target_minutes   = pc["target_duration_minutes"],
        )

        # Save script for debugging
        today_str  = datetime.now().strftime("%Y-%m-%d")
        output_dir = PODCASTS
        output_dir.mkdir(parents=True, exist_ok=True)

        script_path = output_dir / f"{today_str}_script.txt"
        script_path.write_text(script)
        print(f"{GREEN}[PODCAST] Script saved: {script_path}{RESET}", flush=True)

        # ── 3. Audio generation ───────────────────────────────────────────────
        print(f"{GREEN}[PODCAST] Generating audio…{RESET}", flush=True)
        audio_path = str(output_dir / f"{today_str}_daily.mp3")

        from tools.tts.kokoro_tts import generate_audio
        final_path = generate_audio(
            script       = script,
            output_path  = audio_path,
            host_name    = pc["host_name"],
            cohost_name  = pc["cohost_name"],
            host_voice   = pc.get("host_voice",   "af_sky"),
            cohost_voice = pc.get("cohost_voice", "am_adam"),
        )

        # ── 4. Notification ───────────────────────────────────────────────────
        from tools.podcast.notify import send_podcast_notification
        duration = f"~{pc['target_duration_minutes']} min"
        send_podcast_notification(final_path, duration)

        print(f"{GREEN}[PODCAST] Done — {final_path}{RESET}", flush=True)
        return (
            f"Your Daily Brief is ready — "
            f"ARIA and I put together today's rundown: weather, calendar, emails, and the top stories. "
            f"It's opening now. About {pc['target_duration_minutes']} minutes."
        )

    except Exception as exc:
        print(f"{RED}[PODCAST] Failed: {exc}{RESET}", flush=True)
        import traceback; traceback.print_exc()
        return f"Podcast generation hit a snag: {exc}"


def generate_daily_podcast_async() -> str:
    """
    Fire podcast generation in a background thread so ULTRON can confirm
    immediately without blocking the voice pipeline for ~60 seconds.
    """
    def _run():
        generate_daily_podcast()

    threading.Thread(target=_run, daemon=True).start()
    return (
        "Compiling the brief in the background — "
        "weather, news, emails, and calendar. You'll get a notification "
        "when the audio is ready. Try not to miss it."
    )
