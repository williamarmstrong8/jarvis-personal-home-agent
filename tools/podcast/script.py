"""Claude-powered two-host podcast script generator."""
import os
from datetime import datetime

import httpx
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


def generate_podcast_script(
    weather: dict,
    news_text: str,
    email_summary: str,
    calendar_summary: str,
    interests: list,
    host_name: str = "ARIA",
    cohost_name: str = "JARVIS",
    target_minutes: int = 5,
) -> str:
    today         = datetime.now().strftime("%A, %B %d, %Y")
    interests_str = ", ".join(interests)
    word_target   = target_minutes * 150

    prompt = f"""You are writing a script for a daily AI podcast called "The Daily Brief" hosted by two AI assistants: {host_name} (female voice, warm and conversational) and {cohost_name} (male voice — J.A.R.V.I.S., in character).

Today is {today}.

--- WEATHER ---
{weather["summary"]}

--- TODAY'S CALENDAR ---
{calendar_summary}

--- EMAIL HIGHLIGHTS ---
{email_summary}

--- TODAY'S NEWS ---
{news_text}

--- USER INTERESTS ---
{interests_str}

Write a natural, engaging podcast script of approximately {word_target} words (~{target_minutes} min spoken).

FORMAT — use ONLY these speaker labels, nothing else before or after:

{host_name}: [dialogue]
{cohost_name}: [dialogue]

STRUCTURE:
1. Warm intro — {host_name} greets, {cohost_name} teases weather
2. Weather — practical (what to wear, umbrella?)
3. Calendar — what's on today, any prep needed
4. Email highlights — key threads or actions
5. News — 3-4 stories, prioritise user interests, give context not just headlines
6. Close — {cohost_name} witty sign-off, {host_name} signs off

TONE:
- Two smart friends talking, NOT news anchors
- {host_name}: warm, encouraging, slightly playful
- {cohost_name}: in character as J.A.R.V.I.S.
- Reference each other's points — real conversation, not two monologues
- NO filler like "Absolutely!" or "Great question!"
- Opinionated but balanced on news

Output ONLY the script. No markdown, no stage directions, no headers."""

    headers = {
        "x-api-key":         ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type":      "application/json",
    }
    payload = {
        "model":      "claude-opus-4-5",
        "max_tokens": 2500,
        "messages":   [{"role": "user", "content": prompt}],
    }
    with httpx.Client(timeout=60) as client:
        resp = client.post("https://api.anthropic.com/v1/messages",
                           headers=headers, json=payload)
        resp.raise_for_status()
        return resp.json()["content"][0]["text"].strip()
