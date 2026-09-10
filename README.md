# J.A.R.V.I.S. — Just A Rather Very Intelligent System

A fully local, voice-activated AI assistant for macOS. Wake it with "Hey Jarvis", speak your command, and watch the holographic Electron UI react in real time while Claude handles the thinking and Fish Audio handles the voice.

---

## Prerequisites

- **macOS** (Apple Silicon or Intel)
- **Python 3.11+** — `python3 --version`
- **Node.js 18+** — `node --version`
- **Homebrew** — [brew.sh](https://brew.sh)
- **PortAudio** (required by PyAudio):
  ```bash
  brew install portaudio
  ```

---

## 1 — Clone & project setup

```bash
cd ~/Desktop
# The project folder is already here as "Jarvis 2.0"
cd "Jarvis 2.0"
```

---

## 2 — Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> **Note:** On Apple Silicon, if PyAudio fails:
> ```bash
> pip install --global-option='build_ext' \
>   --global-option='-I/opt/homebrew/include' \
>   --global-option='-L/opt/homebrew/lib' pyaudio
> ```

---

## 3 — Node / Electron setup

```bash
npm install
```

---

## 4 — Environment variables

```bash
cp .env.example .env
# Then open .env and fill in every key — instructions for each below
```

---

## 5 — Getting each API key

Wake-word detection uses OpenWakeWord locally and needs no API key.

### Vercel AI Gateway (proxies Claude)
1. Go to [vercel.com/dashboard](https://vercel.com/dashboard) → **AI** → **AI Gateway**
2. Create a gateway, copy the **API Key**
3. Set `VERCEL_AI_GATEWAY_URL=https://ai-gateway.vercel.sh/v1` and `VERCEL_AI_GATEWAY_KEY`

### Fish Audio (voice)
1. Sign up at [fish.audio](https://fish.audio/auth/signup) and create a key at [API Keys](https://fish.audio/app/api-keys)
2. Set `FISH_AUDIO_API_KEY` in `.env`
3. Set `FISH_AUDIO_MODEL_ID` to your cloned voice id (from the Voice Library / your models)
4. Optional: `FISH_AUDIO_TTS_MODEL=s2.1-pro-free` (default, $0). Use `s2.1-pro` once you add [API credit](https://fish.audio/app/developers). Set `TTS_PROVIDER=elevenlabs` to switch back to the ElevenLabs fallback.

### ElevenLabs (voice fallback)
1. Sign up at [elevenlabs.io](https://elevenlabs.io)
2. Go to **Profile → API Key** and copy it
3. Set `ELEVENLABS_API_KEY` in `.env`
4. The default voice ID `pNInz6obpgDQGcFmaJgB` is "Adam" — the closest preset to Jarvis. You can swap to a cloned voice by replacing `ELEVENLABS_VOICE_ID`.

### Spotify
1. Go to [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard)
2. Click **Create App**
3. Set Redirect URI to `http://localhost:8888/callback`
4. Required scopes (set automatically by the code):
   - `user-read-playback-state`
   - `user-modify-playback-state`
   - `user-read-currently-playing`
   - `streaming`
5. Copy **Client ID** and **Client Secret** into `.env`
6. On first run, a browser window will open for OAuth consent — log in and approve

### Gmail
1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a project → **APIs & Services → Enable APIs** → search **Gmail API** → enable it
3. **APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID**
4. Application type: **Desktop app**
5. Download the JSON file and save it as `credentials/gmail_credentials.json`
6. Set `GMAIL_CREDENTIALS_PATH=./credentials/gmail_credentials.json` in `.env`
7. First run: a browser window opens for OAuth consent — log in and approve. Token is saved as `credentials/gmail_token.json` for future runs.

### Notion
1. Go to [notion.so/my-integrations](https://www.notion.so/my-integrations)
2. Click **New integration**, give it a name (e.g. "Jarvis")
3. Copy the **Internal Integration Token** → set as `NOTION_API_KEY`
4. In Notion, open each page you want Jarvis to access → **⋯ menu → Add connections → Jarvis**
5. Optionally set `NOTION_DEFAULT_PAGE_ID` to a parent page ID (copy from the page's URL)
6. Optionally set `NOTION_CONTENT_DB_ID` for generated LinkedIn/X drafts

### Homelab (Raspberry Pi over Tailscale)
Real hostnames and tokens stay in `.env` only — never in source.

1. Set `PI_MCP_URL` to your Pi MCP endpoint (Tailscale Funnel or MagicDNS) and `PI_MCP_TOKEN`
2. Set `JELLYFIN_URL` + `JELLYFIN_API_KEY` to play movies/TV in the Mac browser
3. Optional: `HOMELAB_HOST` if you want “open Radarr / Sonarr / Plex / HA” without Jellyfin (otherwise the host is taken from `JELLYFIN_URL`)

---

## 6 — Running Jarvis

**Dev (terminal):**

```bash
source .venv/bin/activate   # if not already active
python main.py
```

**Mac app (Applications, click to launch — not at login):**

```bash
npm run install:app
```

That builds `Jarvis.app` and copies it to `/Applications`. It does **not** add a login item and does **not** sit in the Dock — look for a colored dot in the menu bar (next to Wi‑Fi). Click it for last heard / reply; right-click → Quit Jarvis.

First launch: grant **Microphone** (and Screen Recording if you use that). If macOS blocks the app, right-click → Open.

Python code, `.env`, and skills pick up on the next launch — no rebuild. Rebuild with `npm run install:app` after UI/Electron changes.

Do not run `python main.py` and the .app at the same time — they fight over the mic and port 8765.

---

## 7 — Say "Hey Jarvis"

The menu bar dot turns green while it listens. Try these commands:

| Command | What happens |
|---|---|
| "Hey Jarvis, play some Radiohead" | Plays Radiohead's top tracks on Spotify |
| "Hey Jarvis, what's playing?" | Reads back the current track |
| "Hey Jarvis, skip this" | Skips to next track |
| "Hey Jarvis, pause the music" | Pauses Spotify |
| "Hey Jarvis, draft an email to john@example.com saying I'll be late to the meeting" | Creates a Gmail draft |
| "Hey Jarvis, send an email to jane@example.com subject hello body just checking in" | Sends immediately |
| "Hey Jarvis, create a Notion page called Project Ideas with a brief outline" | Creates Notion page |
| "Hey Jarvis, search Notion for meeting notes" | Returns top matching pages |
| "Hey Jarvis, what time is it in Tokyo?" | Conversational answer |

---

## 8 — Testing integrations independently

Run the non-mutating unit suite first:

```bash
python -m unittest discover -s tests -p 'test_*.py'
```

The following smoke tests contact real integrations and may create data or
start playback:

```bash
python main.py --test-spotify   # plays music, shows now_playing
python main.py --test-gmail     # creates a test draft
python main.py --test-notion    # searches + creates a test page
python main.py --test-screen    # captures the display (needs Screen Recording)
python main.py --test-pi-mcp    # lists Pi tools and calls get_status
```

The Pi now-playing daemon lives at `tools/pi-display/display.py`; changes to
that file must be deployed to the Pi and its `spotify-display` service
restarted. While playing, it reconciles with Spotify every two seconds by
default (`PI_DISPLAY_POLL_PLAYING`) while direct play/skip commands use faster
short-lived synchronization from the Mac.

For STT benchmarking, collect representative 16 kHz WAV commands in one
directory. Add a same-name `.txt` transcript beside each WAV to calculate word
error rate, then run:

```bash
python scripts/benchmark-stt.py ./command-corpus
```

Configure `WHISPER_CPP_BIN` and `WHISPER_CPP_MODEL` to include whisper.cpp in
the comparison. `STT_BACKEND=auto` uses it when available and safely falls back
to the existing `base.en` backend.

With whisper.cpp selected, `STT_STREAMING=auto` transcribes rolling audio
snapshots while the user speaks. Partial text updates the UI and prebuilds the
likely route, but never executes a command. During endpoint silence Jarvis
starts final candidates early and reuses one only when the snapshot contains
the last frame classified as speech plus nearly all tail audio. Set
`STT_STREAMING=off` to compare against the full-file path. `STT_STREAMING=on`
also permits rolling Python Whisper for experiments, but `auto` avoids it
because repeated CPU inference can be slower.

Completed turns record stage timings in `data/logs/latency.jsonl`. Summarize
median and p95 latency by intent with:

```bash
python scripts/latency-report.py
```

The report separates post-speech delay into STT, response/tool work, and TTS.
Local context questions and high-confidence commands skip the response model;
small-talk turns omit tool schemas so their text can stream directly to speech.
`VAD_FRAME_MS`, `VAD_SILENCE_SECS`, `TTS_PREROLL_MS`, and
`SPOTIFY_DUCK_SETTLE_SECS` are latency/accuracy controls. Tune them against the
same recorded command corpus and compare both p95 latency and word-error rate.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `PyAudio` install fails | `brew install portaudio` first |
| `No Spotify player on this Mac` | Install the Spotify desktop app on **this** Mac. Jarvis plays here, not on another computer that was last using Spotify. Override the device name with `SPOTIFY_DEVICE_NAME` if it still picks wrong. |
| Gmail OAuth browser doesn't open | Run `python main.py` in a visible terminal (not a headless session) |
| Fish Audio quota exceeded | Jarvis falls back to ElevenLabs, then `pyttsx3` system TTS |
| Whisper is slow on first run | It downloads the `base` model (~140 MB) once — subsequent runs use the cache |
| Wake word sensitivity too low | Tune `WAKE_THRESHOLD` in `.env`; lower values are more sensitive but increase false activations |
| UI doesn't appear | Ensure `npm install` was run in the project root; check terminal for Electron errors |

---

## File structure

```
Jarvis 2.0/
├── main.py                 # Entry point — wake word loop
├── core/                   # Brain, speech, intent, presence
│   ├── brain.py
│   ├── intent.py
│   ├── speech.py
│   ├── context.py
│   ├── ambient.py
│   ├── audio_duck.py
│   ├── ws_server.py
│   ├── suit_up.py
│   └── skills.py
├── integrations/           # External services
│   ├── spotify.py
│   ├── gmail.py
│   ├── calendar.py
│   ├── notion.py
│   ├── messages.py
│   ├── search.py
│   ├── content.py
│   ├── jellyfin.py
│   ├── homelab_web.py
│   ├── pi_mcp.py
│   └── screen.py
├── ui/                     # Electron HUD
│   ├── main.js
│   ├── preload.js
│   ├── index.html
│   ├── style.css
│   └── renderer.js
├── skills/                 # Drop-in SKILL.md agents
├── tools/                  # Podcast pipeline, TTS, data sources, Pi MCP
│   ├── podcast/
│   ├── tts/
│   ├── data_sources/
│   └── pi-mcp/
├── config/
├── credentials/            # OAuth JSON (gitignored)
├── data/                   # Logs, caches, models, podcast output
├── assets/sounds/
├── tests/
├── .env                    # Secrets — copy from .env.example, never commit
├── .env.example
├── requirements.txt
├── package.json
└── README.md
```
# jarvis-personal-home-agent
