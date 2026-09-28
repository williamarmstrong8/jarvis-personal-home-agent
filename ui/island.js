// Jarvis notch island — grows out of the camera housing and shows, live:
// what you're saying, what Jarvis is doing (tools), and what he says back.

const WS_URL = "ws://127.0.0.1:8765";
const RECONNECT_MS = 2000;
const LINGER_MS = 6500;          // stay open after a reply finishes
const LINGER_CARD_MS = 11000;    // …longer when there's a card to look at
const HOVER_OPEN_MS = 260;
const HOVER_CLOSE_MS = 450;
const SPOTIFY_REFRESH_MS = 8000;

const $ = (id) => document.getElementById(id);
const island = $("island");
const bodyInner = $("bodyInner");
const youEl = $("you");
const replyEl = $("reply");
const activityEl = $("activity");
const bars = [...$("bars").children];
const composer = $("composer");
const input = $("composerInput");
const spotifyCard = $("spotifyCard");
const mediaCard = $("mediaCard");

const ui = {
  state: "offline",
  mode: "collapsed",
  notchH: 37,
  hoverOpen: false,
  composing: false,
  lingerUntil: 0,
  lingerTimer: null,
  hoverTimer: null,
  interactive: false,
  youWords: [],
  replyText: "",
  running: new Set(),
  spotify: null,
  spotifyAt: 0,
  lastRefresh: 0,
};

let ws = null;

// ── Tool labels (SF-Symbols-ish tint + glyph per family) ─────────────────────

const TOOL_META = {
  play_spotify:          ["Spotify", "Playing", "#1ed760", "♫"],
  pause_spotify:         ["Spotify", "Pausing", "#1ed760", "♫"],
  resume_spotify:        ["Spotify", "Resuming", "#1ed760", "♫"],
  skip_spotify:          ["Spotify", "Skipping track", "#1ed760", "♫"],
  get_currently_playing: ["Spotify", "Checking what's playing", "#1ed760", "♫"],
  draft_gmail:           ["Mail", "Drafting email", "#0a84ff", "✉"],
  send_gmail:            ["Mail", "Sending email", "#0a84ff", "✉"],
  search_gmail:          ["Mail", "Searching inbox", "#0a84ff", "✉"],
  read_email:            ["Mail", "Reading email", "#0a84ff", "✉"],
  list_calendar_events:  ["Calendar", "Checking calendar", "#ff453a", "▦"],
  create_calendar_event: ["Calendar", "Adding event", "#ff453a", "▦"],
  create_notion_page:    ["Notion", "Creating page", "#8e8e93", "N"],
  search_notion:         ["Notion", "Searching", "#8e8e93", "N"],
  append_to_notion:      ["Notion", "Updating page", "#8e8e93", "N"],
  web_search:            ["Web", "Searching the web", "#64d2ff", "⌕"],
  generate_content:      ["Content", "Drafting posts", "#5e5ce6", "✎"],
  send_imessage:         ["Messages", "Sending message", "#30d158", "💬"],
  search_imessage:       ["Messages", "Searching messages", "#30d158", "💬"],
  airdrop_file:          ["AirDrop", "Sharing file", "#0a84ff", "⇪"],
  generate_daily_podcast:["Podcast", "Producing today's brief", "#bf5af2", "◉"],
  look_at_screen:        ["Screen", "Looking at your screen", "#ff9f0a", "◧"],
  suit_up:               ["Jarvis", "Suit-up sequence", "#ff9f0a", "⚡"],
  open_homelab:          ["Homelab", "Opening", "#ff9f0a", "⌂"],
  play_movie:            ["Jellyfin", "Starting playback", "#bf5af2", "▶"],
  control_pi_display:    ["Pi Display", "Controlling playback", "#bf5af2", "▶"],
};

function toolMeta(name) {
  if (TOOL_META[name]) return TOOL_META[name];
  if (name && name.startsWith("pi_")) {
    const words = name.slice(3).replace(/_/g, " ");
    return ["Homelab", words.charAt(0).toUpperCase() + words.slice(1), "#ff9f0a", "⌂"];
  }
  return ["Jarvis", (name || "Working").replace(/_/g, " "), "#636366", "•"];
}

const SPOTIFY_TOOLS = new Set([
  "play_spotify", "pause_spotify", "resume_spotify", "skip_spotify", "get_currently_playing",
]);

// ── Layout ───────────────────────────────────────────────────────────────────

function hasContent() {
  return Boolean(
    ui.youWords.length || ui.replyText || activityEl.children.length
    || !spotifyCard.hidden || !mediaCard.hidden,
  );
}

function desiredMode() {
  if (ui.state === "offline" && !ui.composing && !ui.hoverOpen) return "collapsed";
  if (ui.composing || ui.hoverOpen) return "expanded";
  const active = ["listening", "thinking", "speaking"].includes(ui.state);
  if (active) return hasContent() ? "expanded" : "compact";
  if (Date.now() < ui.lingerUntil && hasContent()) return "expanded";
  return "collapsed";
}

function measure() {
  if (ui.mode !== "expanded") return;
  const max = window.innerHeight - 20;
  const h = Math.min(max, ui.notchH + bodyInner.scrollHeight);
  island.style.setProperty("--h", `${h}px`);
}

function refresh() {
  const mode = desiredMode();
  if (mode !== ui.mode) {
    ui.mode = mode;
    island.classList.toggle("is-collapsed", mode === "collapsed");
    island.classList.toggle("is-compact", mode === "compact");
    island.classList.toggle("is-expanded", mode === "expanded");
    if (mode === "expanded") measure();
    if (mode === "collapsed" && document.activeElement === input) input.blur();
  }
  island.classList.toggle("is-busy", ui.state === "thinking" || ui.running.size > 0);
}

new ResizeObserver(measure).observe(bodyInner);

function setState(state) {
  const prev = ui.state;
  ui.state = state;
  island.classList.remove(
    "state-idle", "state-listening", "state-thinking", "state-speaking", "state-offline",
  );
  island.classList.add(`state-${state}`);
  window.jarvis?.setState(state);
  if (state !== "listening") setLevel(0);
  if (state === "idle" && prev !== "idle" && prev !== "offline") {
    linger(!spotifyCard.hidden || !mediaCard.hidden ? LINGER_CARD_MS : LINGER_MS);
  }
  refresh();
}

function linger(ms) {
  ui.lingerUntil = Date.now() + ms;
  clearTimeout(ui.lingerTimer);
  ui.lingerTimer = setTimeout(refresh, ms + 20);
}

// ── Mic level → waveform ─────────────────────────────────────────────────────

const BAR_PROFILE = [0.55, 0.85, 1, 0.8, 0.6];

function setLevel(value) {
  const v = Math.max(0, Math.min(1, value));
  bars.forEach((bar, i) => {
    const jitter = 0.75 + Math.random() * 0.5;
    const s = ui.state === "listening" ? 0.2 + v * BAR_PROFILE[i] * jitter * 0.8 : 0.25;
    bar.style.setProperty("--s", Math.min(1, s).toFixed(3));
  });
}

// ── Live transcript, word by word ────────────────────────────────────────────

function escapeHtml(text) {
  return text.replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

function setYou(text, { partial = false, placeholder = false } = {}) {
  const words = (text || "").trim().split(/\s+/).filter(Boolean);
  let keep = 0;
  while (keep < words.length && keep < ui.youWords.length && words[keep] === ui.youWords[keep]) {
    keep += 1;
  }
  youEl.innerHTML = words.map((w, i) => {
    const cls = i >= keep && !placeholder ? "w fresh" : "w";
    return `<span class="${cls}">${escapeHtml(w)}${i < words.length - 1 ? " " : ""}</span>`;
  }).join("");
  ui.youWords = placeholder ? [] : words;
  youEl.classList.toggle("is-partial", partial);
  youEl.classList.toggle("is-final", !partial && !placeholder && words.length > 0);
  youEl.classList.toggle("placeholder", placeholder);
  refresh();
}

// ── Reply ────────────────────────────────────────────────────────────────────

function renderReply() {
  const clean = ui.replyText.replace(/\s*\[FOLLOWUP\]\s*/g, " ").replace(/\s+/g, " ").trim();
  replyEl.textContent = clean;
  refresh();
}

function resetReply() {
  ui.replyText = "";
  renderReply();
}

// ── Activity rows ────────────────────────────────────────────────────────────

function actRow(id, name) {
  let row = activityEl.querySelector(`[data-id="${CSS.escape(id)}"]`);
  if (row) return row;
  const [app, label, tint, glyph] = toolMeta(name);
  row = document.createElement("div");
  row.className = "act running";
  row.dataset.id = id;
  row.dataset.name = name;
  row.innerHTML = `
    <div class="act-icon" style="--tint:${tint}">${escapeHtml(glyph)}</div>
    <div class="act-text">
      <div class="act-label">${escapeHtml(label)}</div>
      <div class="act-detail">${escapeHtml(app)}</div>
    </div>
    <div class="act-status"></div>
    <div class="act-summary"></div>`;
  activityEl.appendChild(row);
  ui.running.add(id);
  refresh();
  return row;
}

function toolStarted(data) {
  const id = data.id || `${data.name}-${Date.now()}`;
  const row = actRow(id, data.name);
  if (data.detail) {
    const [app] = toolMeta(data.name);
    row.querySelector(".act-detail").textContent = `${app} · ${data.detail}`;
  }
}

function toolFinished(data) {
  const id = data.id || [...ui.running].find((r) => r.startsWith(data.name));
  if (!id) return;
  const row = actRow(id, data.name);
  row.classList.remove("running");
  row.classList.add(data.ok === false ? "failed" : "done");
  ui.running.delete(id);
  // Spotify results are shown by the card; others get a short summary.
  if (data.summary && !SPOTIFY_TOOLS.has(data.name)) {
    row.querySelector(".act-summary").textContent = data.summary;
  }
  refresh();
}

function clearActivity() {
  activityEl.innerHTML = "";
  ui.running.clear();
}

// ── Cards ────────────────────────────────────────────────────────────────────

function fmt(ms) {
  const s = Math.max(0, Math.floor((ms || 0) / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function showSpotify(data) {
  ui.spotify = data;
  ui.spotifyAt = performance.now();
  spotifyCard.hidden = false;
  const art = $("spArt");
  if (data.art && art.getAttribute("src") !== data.art) art.src = data.art;
  art.hidden = !data.art;
  // The card says it all — drop the successful Spotify rows it replaces.
  activityEl.querySelectorAll(".act.done").forEach((row) => {
    if (SPOTIFY_TOOLS.has(row.dataset.name)) row.remove();
  });
  $("spTitle").textContent = data.title || "";
  $("spArtist").textContent = data.artist || "";
  $("spStatus").textContent = data.is_playing ? "Playing" : "Paused";
  spotifyCard.classList.toggle("paused", !data.is_playing);
  $("spShuffle").classList.toggle("on", Boolean(data.shuffle));
  $("spRepeat").classList.toggle("on", data.repeat && data.repeat !== "off");
  $("spDur").textContent = fmt(data.duration_ms);
  tickSpotify();
  refresh();
}

function tickSpotify() {
  const d = ui.spotify;
  if (!d || spotifyCard.hidden) return;
  const elapsed = d.is_playing ? performance.now() - ui.spotifyAt : 0;
  const pos = Math.min(d.duration_ms || 0, (d.progress_ms || 0) + elapsed);
  const pct = d.duration_ms ? (pos / d.duration_ms) * 100 : 0;
  $("spFill").style.width = `${pct}%`;
  $("spKnob").style.left = `${pct}%`;
  $("spPos").textContent = fmt(pos);
  if (ui.mode === "expanded" && Date.now() - ui.lastRefresh > SPOTIFY_REFRESH_MS) {
    ui.lastRefresh = Date.now();
    send({ type: "media", action: "refresh" });
  }
}
setInterval(tickSpotify, 500);

function showMedia(data) {
  mediaCard.hidden = false;
  const ep = data.season && data.episode ? ` · S${data.season} E${data.episode}` : "";
  $("mediaTitle").textContent = `${data.title || "Now playing"}${ep}`;
  $("mediaSub").textContent = data.status || `Playing on ${data.where || "Mac"}`;
  refresh();
}

function hideCards() {
  spotifyCard.hidden = true;
  mediaCard.hidden = true;
}

// ── Turn lifecycle ───────────────────────────────────────────────────────────

function newTurn({ followup = false } = {}) {
  clearTimeout(ui.lingerTimer);
  ui.lingerUntil = 0;
  if (!followup) {
    resetReply();
    clearActivity();
    hideCards();
  }
  setYou("Listening…", { placeholder: true });
}

function handleEvent(data) {
  switch (data.event) {
    case "wake":
    case "listening":
      if (ui.state !== "listening") newTurn();
      setState("listening");
      break;
    case "followup_listening":
      newTurn({ followup: true });
      setState("listening");
      break;
    case "level":
      if (ui.state === "listening") setLevel(data.value || 0);
      break;
    case "partial_transcript":
      if (data.text) setYou(data.text, { partial: true });
      break;
    case "transcript":
      if (data.source === "typed") {
        clearActivity();
        hideCards();
      }
      setYou(data.text || "", { partial: false });
      break;
    case "thinking":
      setState("thinking");
      break;
    case "reply_start":
      resetReply();
      break;
    case "reply_delta":
      ui.replyText += data.text || "";
      renderReply();
      break;
    case "speaking":
      setState("speaking");
      break;
    case "spoke":
      if (!ui.replyText && data.text) {
        ui.replyText = data.text;
        renderReply();
      }
      break;
    case "tool_pending":
    case "tool":
      toolStarted(data);
      break;
    case "tool_done":
      toolFinished(data);
      break;
    case "card":
      if (data.kind === "spotify" && data.data) showSpotify(data.data);
      else if (data.kind === "media" && data.data) showMedia(data.data);
      break;
    case "idle":
      if (youEl.classList.contains("placeholder")) setYou("");
      setState("idle");
      break;
  }

  switch (data.type) {
    case "suit_up_start":
      newTurn();
      setYou("");
      setState("thinking");
      break;
    case "suit_up_phase":
      toolFinished({ id: "suit-phase", name: "suit_up", ok: true });
      toolStarted({ id: "suit-phase", name: "suit_up", detail: titleCase(data.label || "") });
      break;
    case "suit_up_check":
      toolStarted({ id: `suit-${data.item}`, name: "suit_up", detail: titleCase(data.item || "") });
      toolFinished({ id: `suit-${data.item}`, name: "suit_up", ok: data.status === "OK" });
      break;
    case "suit_up_complete":
      toolFinished({ id: "suit-phase", name: "suit_up", ok: true });
      setState("idle");
      break;
  }
}

function titleCase(text) {
  return text.toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase());
}

// ── WebSocket ────────────────────────────────────────────────────────────────

function send(message) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(message));
}

function connect() {
  ws = new WebSocket(WS_URL);
  ws.addEventListener("open", () => setState("idle"));
  ws.addEventListener("message", (e) => {
    try {
      const data = JSON.parse(e.data);
      if (data.event || data.type) handleEvent(data);
    } catch { /* ignore malformed frames */ }
  });
  ws.addEventListener("close", () => {
    setState("offline");
    setTimeout(connect, RECONNECT_MS);
  });
  ws.addEventListener("error", () => ws.close());
}

// ── Pointer: click-through everywhere except the island ──────────────────────

function islandHit(x, y) {
  const r = island.getBoundingClientRect();
  const ear = ui.mode === "expanded" ? 16 : 12;
  // Collapsed, the notch itself is the target — pad it so it's easy to hit.
  const pad = ui.mode === "collapsed" ? 6 : 0;
  return x >= r.left - ear - pad && x <= r.right + ear + pad && y >= 0 && y <= r.bottom + pad;
}

function setInteractive(on) {
  if (ui.interactive === on) return;
  ui.interactive = on;
  window.jarvis?.setInteractive(on);
}

function setHover(on) {
  clearTimeout(ui.hoverTimer);
  if (on && !ui.hoverOpen) {
    ui.hoverTimer = setTimeout(() => {
      ui.hoverOpen = true;
      refresh();
    }, HOVER_OPEN_MS);
  } else if (!on && ui.hoverOpen) {
    ui.hoverTimer = setTimeout(() => {
      ui.hoverOpen = false;
      refresh();
    }, HOVER_CLOSE_MS);
  }
}

document.addEventListener("mousemove", (e) => {
  const inside = islandHit(e.clientX, e.clientY);
  setInteractive(inside || ui.composing);
  setHover(inside);
});
document.addEventListener("mouseleave", () => {
  if (!ui.composing) setInteractive(false);
  setHover(false);
});

island.addEventListener("click", (e) => {
  if (e.target.closest("button, input, kbd")) return;
  clearTimeout(ui.hoverTimer);
  ui.hoverOpen = true;
  refresh();
});
island.addEventListener("contextmenu", (e) => {
  e.preventDefault();
  window.jarvis?.contextMenu();
});

// ── Controls ─────────────────────────────────────────────────────────────────

$("stopBtn").addEventListener("click", () => send({ type: "stop" }));

spotifyCard.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-media]");
  if (!btn) return;
  const action = btn.dataset.media;
  if (action === "toggle" && ui.spotify) {
    // Optimistic — the backend pushes the real state right after.
    const d = ui.spotify;
    showSpotify({ ...d, is_playing: !d.is_playing, progress_ms: d.progress_ms + (d.is_playing ? performance.now() - ui.spotifyAt : 0) });
  }
  send({ type: "media", action });
});

function talk() {
  send({ type: "listen" });
}
$("hotkeyHint").addEventListener("click", talk);

function openComposer() {
  ui.composing = true;
  refresh();
  window.jarvis?.focusInput();
  setTimeout(() => input.focus(), 60);
}

input.addEventListener("focus", () => {
  ui.composing = true;
  refresh();
});
input.addEventListener("mousedown", () => window.jarvis?.focusInput());
input.addEventListener("blur", () => {
  ui.composing = false;
  refresh();
});
input.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    input.value = "";
    input.blur();
    ui.hoverOpen = false;
    refresh();
  }
});
composer.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  resetReply();
  clearActivity();
  setYou(text);
  setState("thinking");
  send({ type: "text", text });
});

window.jarvis?.on("geometry", (geo) => {
  ui.notchH = geo.height;
  document.documentElement.style.setProperty("--notch-w", `${geo.width}px`);
  document.documentElement.style.setProperty("--notch-h", `${geo.height}px`);
  island.classList.toggle("no-notch", !geo.hasNotch);
  measure();
});
window.jarvis?.on("hotkey", talk);
window.jarvis?.on("compose", openComposer);
window.jarvis?.on("window-blur", () => {
  if (document.activeElement === input) input.blur();
  ui.hoverOpen = false;
  refresh();
});

setState("offline");
connect();
