"""macOS notification + auto-open for the generated podcast."""
import os
import subprocess
from datetime import datetime

GREEN = "\033[92m"
RESET = "\033[0m"


def send_podcast_notification(audio_path: str, duration_estimate: str = "~5 min"):
    abs_path = os.path.abspath(audio_path)
    date_str = datetime.now().strftime("%A, %B %d")

    # Try terminal-notifier (brew install terminal-notifier) for click-to-open
    try:
        result = subprocess.run([
            "terminal-notifier",
            "-title",   "JARVIS Daily Brief",
            "-subtitle", date_str,
            "-message",  f"Your podcast is ready ({duration_estimate}). Click to play.",
            "-open",    f"file://{abs_path}",
            "-sound",   "Blow",
        ], capture_output=True, timeout=5)
        if result.returncode == 0:
            print(f"{GREEN}[NOTIFY] Sent via terminal-notifier{RESET}", flush=True)
            return
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Fallback: AppleScript notification + auto-open
    script = f'''
    display notification "Your Daily Brief is ready ({duration_estimate}). Opening now..." ¬
        with title "JARVIS Daily Brief" ¬
        subtitle "{date_str}" ¬
        sound name "Blow"
    delay 1
    open POSIX file "{abs_path}"
    '''
    subprocess.run(["osascript", "-e", script], capture_output=True)
    print(f"{GREEN}[NOTIFY] Notification sent + file opening{RESET}", flush=True)
