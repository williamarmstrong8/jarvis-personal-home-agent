const WS_URL = "ws://localhost:8765";
const RECONNECT_MS = 2000;

const LABELS = {
  idle:      "Standby",
  listening: "Listening",
  thinking:  "Thinking",
  speaking:  "Speaking",
  offline:   "Connecting…",
};

const statusLabel = document.getElementById("statusLabel");
const heardEl = document.getElementById("heard");
const replyEl = document.getElementById("reply");
const toolEl = document.getElementById("tool");

function setStatus(name) {
  const state = LABELS[name] ? name : "idle";
  document.body.className = `state-${state}`;
  statusLabel.textContent = LABELS[state];
  window.jarvis?.setState(state);
}

function handleEvent(data) {
  switch (data.event) {
    case "wake":
    case "listening":
    case "followup_listening":
      setStatus("listening");
      break;
    case "thinking":
      setStatus("thinking");
      break;
    case "speaking":
      setStatus("speaking");
      break;
    case "idle":
      setStatus("idle");
      break;
    case "transcript":
      if (data.text) heardEl.textContent = data.text;
      break;
    case "partial_transcript":
      if (data.text) heardEl.textContent = data.text;
      break;
    case "spoke":
      if (data.text) replyEl.textContent = data.text;
      break;
    case "tool": {
      const name = data.name || "tool";
      const detail = data.detail ? ` — ${data.detail}` : "";
      toolEl.textContent = `${name}${detail}`;
      break;
    }
  }
}

let ws = null;

function connect() {
  ws = new WebSocket(WS_URL);
  ws.addEventListener("open", () => setStatus("idle"));
  ws.addEventListener("message", (e) => {
    try {
      const data = JSON.parse(e.data);
      if (data.event) handleEvent(data);
    } catch { /* ignore */ }
  });
  ws.addEventListener("close", () => {
    setStatus("offline");
    setTimeout(connect, RECONNECT_MS);
  });
  ws.addEventListener("error", () => ws.close());
}

document.getElementById("quitBtn").addEventListener("click", () => {
  window.jarvis?.quit();
});

setStatus("offline");
connect();
