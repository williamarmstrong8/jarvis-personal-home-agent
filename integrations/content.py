"""
J.A.R.V.I.S. — Content Generation
Generates LinkedIn + X posts in Will Armstrong's voice, saves to Notion content database.
"""

import os
from datetime import datetime

import httpx
from dotenv import load_dotenv

load_dotenv()

GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
RESET  = "\033[0m"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CONTENT_DB_ID     = (os.environ.get("NOTION_CONTENT_DB_ID") or "").strip()

# ── Will Armstrong's writing style, baked in from real posts ──────────────────

WILL_STYLE_GUIDE = """\
You are writing LinkedIn posts for Will Armstrong. You MUST match his exact voice, tone, and structure.

WILL'S STYLE — learned from his real posts:

STRUCTURE:
- Open with a standalone hook: a bold one-liner, statement, or personal reveal. It should hit hard on its own.
- Follow with short context: 1-2 sentences of personal framing ("I've been building...", "For the past X months...")
- Build the body: personal story or observation → specific details → extracted insight
- Use bullet points with • for lists of tools, observations, or steps
- End with: a direct question to the audience OR a tight confident close (no "let me know your thoughts")

SENTENCE STYLE:
- Short sentences. Very short. Often 3-7 words.
- Single words or phrases on their own line for emphasis ("Systems first." / "Taste is." / "Frictionless automation.")
- Never use corporate speak, buzzword salad, or filler phrases
- No emojis
- 0-2 hashtags max, only if genuinely relevant, placed at the very end
- No sign-off ("Thanks", "DM me", etc.)

TONE:
- Confident but not arrogant
- Curious and builder-minded
- Personal — "I built", "I've been", "I wanted"
- Direct — says the thing, doesn't dance around it

THEMES (weave in naturally when relevant):
- Systems, automation, frictionless workflows
- AI as a tool for leverage, not replacement
- Building in public, iteration over perfection
- GTM engineering, data pipelines, n8n, Clay, Notion
- Health data, pattern recognition
- Product taste, design judgment

LENGTH: 150-280 words. No padding.

REAL POST EXAMPLES FOR CALIBRATION:

Example 1 (hook → personal → insight):
"I automated Apple's Action Button to actually work.

I wanted one entry point for everything. No app switching, no friction, no thinking about where to put it. One press, I speak, and the system routes to one of three things:

Log a meal: AI pulls out the foods, calculates calories, and tags anything inflammatory or allergenic.
Log a symptom: After eating, if something feels off, I update the log.
Capture a note: I have ideas constantly. Most of them die between the thought and finding somewhere to write them down.

The whole thing runs on n8n, with AI nodes filtering and cleaning the data before it ever hits my database.

I built this because consistency is the hardest part of any health habit. Remove the friction, and consistency takes care of itself.

Frictionless automation. Data, delivered."

Example 2 (bold take → elaboration → question):
"The most important hire in modern sales might be a GTM Engineer.

For most of my work, I've lived between worlds.
- Engineering.
- Business.
- People.

Recently I came across the role GTM Engineer and it immediately clicked.
It's the person who builds the infrastructure behind revenue.

Current stack looks something like:
• Clay
• Supabase
• Attio
• n8n

Curious what others in this space are building.
What does your GTM stack look like right now?"

Example 3 (short, punchy, builds to insight):
"AI can build. But AI doesn't have taste.

In a world where anyone can spin up a website or startup, the real differentiator is:
• Does it look polished and on brand?
• Does the experience feel smooth from click to conversion?

AI accelerates execution.
It doesn't replace judgment."
"""

LINKEDIN_PROMPT = """\
{style_guide}

Now write a LinkedIn post about the following idea from Will:

IDEA: {idea}

Write ONLY the post. No preamble, no explanation, no "Here's a post:", no quotes around it. Just the post itself, ready to copy-paste.
"""

X_PROMPT = """\
You are writing an X (Twitter) post for Will Armstrong.

His X voice is the same as LinkedIn but compressed:
- Max 280 characters (hard limit)
- Hook in the first line
- Drop the fluff, keep the punch
- 0-1 hashtags max
- Can be the core insight from a longer LinkedIn post

IDEA: {idea}
LINKEDIN POST (for context): {linkedin_post}

Write ONLY the X post. No preamble. Just the text, under 280 characters.
"""


def _call_claude(prompt: str) -> str:
    """Call Claude API directly (synchronous) for content generation."""
    headers = {
        "x-api-key":         ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type":      "application/json",
    }
    payload = {
        "model":      "claude-opus-4-5",
        "max_tokens": 1024,
        "messages":   [{"role": "user", "content": prompt}],
    }
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"].strip()


def _save_to_notion(idea: str, linkedin_post: str, x_post: str) -> str:
    """Save the generated content to the Notion content database."""
    if not CONTENT_DB_ID:
        return "Notion content database isn't configured (NOTION_CONTENT_DB_ID)."
    try:
        from notion_client import Client
        n = Client(auth=os.environ.get("NOTION_API_KEY", ""))

        today = datetime.now().strftime("%Y-%m-%d")

        page = n.pages.create(
            parent={"type": "database_id", "database_id": CONTENT_DB_ID},
            properties={
                "Idea Overview": {
                    "title": [{"type": "text", "text": {"content": idea[:100]}}]
                },
                "Idea ": {
                    "rich_text": [{"type": "text", "text": {"content": idea}}]
                },
                "Date": {
                    "date": {"start": today}
                },
                "Linkedin Post": {
                    "rich_text": [{"type": "text", "text": {"content": linkedin_post}}]
                },
                "X Post": {
                    "rich_text": [{"type": "text", "text": {"content": x_post}}]
                },
                "Status": {
                    "select": {"name": "Draft"}
                },
            },
        )
        url = page.get("url", "")
        print(f"{GREEN}[CONTENT] Saved to Notion content DB: {url}{RESET}", flush=True)
        return url
    except Exception as exc:
        print(f"{RED}[CONTENT] Notion save failed: {exc}{RESET}", flush=True)
        raise


def generate_content(idea: str) -> str:
    """
    Generate a LinkedIn post + X post in Will's voice from a spoken idea,
    then save both to his Notion content database.
    Returns a confirmation string for JARVIS to speak.
    """
    try:
        print(f"{GREEN}[CONTENT] Generating LinkedIn post for: {idea[:60]}…{RESET}", flush=True)

        # Generate LinkedIn post
        linkedin_post = _call_claude(
            LINKEDIN_PROMPT.format(style_guide=WILL_STYLE_GUIDE, idea=idea)
        )
        print(f"{GREEN}[CONTENT] LinkedIn post generated ({len(linkedin_post)} chars){RESET}", flush=True)

        # Generate X post (using the LinkedIn post for context/compression)
        x_post = _call_claude(
            X_PROMPT.format(idea=idea, linkedin_post=linkedin_post)
        )
        print(f"{GREEN}[CONTENT] X post generated ({len(x_post)} chars){RESET}", flush=True)

        # Save to Notion
        url = _save_to_notion(idea, linkedin_post, x_post)

        return (
            f"Done, sir. LinkedIn post drafted in your voice and saved to your content database. "
            f"X post compressed alongside it — both sitting in Notion as drafts, ready for your review."
        )

    except Exception as exc:
        print(f"{RED}[CONTENT] generate_content failed: {exc}{RESET}", flush=True)
        return f"Content generation hit a snag, sir: {exc}"
