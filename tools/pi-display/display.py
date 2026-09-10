#!/usr/bin/env python3
"""Pi HDMI display: Spotify now-playing, agent cards, local video.

Mac Jarvis pushes state over HTTP (:8766) so this process never waits on
the Spotify API for commanded track changes. A short adaptive poll keeps
manual changes, buffering, and progress reconciled.
"""
from __future__ import annotations

import base64
import io
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

CONF = "/etc/spotify-display.json"
FB = "/dev/fb0"
BLANK = "/sys/class/graphics/fb0/blank"
POLL_PLAYING = max(0.75, float(os.environ.get("PI_DISPLAY_POLL_PLAYING", "2.0")))
POLL_IDLE = max(2.0, float(os.environ.get("PI_DISPLAY_POLL_IDLE", "5.0")))
PUSH_FRESH = max(0.25, float(os.environ.get("PI_DISPLAY_PUSH_FRESH", "0.75")))
FPS = 15.0
HTTP_PORT = int(os.environ.get("PI_DISPLAY_PORT", "8766"))
TOKEN_FILE = os.environ.get("PI_DISPLAY_TOKEN_FILE", "/etc/pi-mcp-token")
VLC_RC_HOST = os.environ.get("PI_DISPLAY_VLC_RC_HOST", "127.0.0.1")
VLC_RC_PORT = int(os.environ.get("PI_DISPLAY_VLC_RC_PORT", "4212"))

BG = (10, 11, 14)
FGC = (238, 240, 244)
MUT = (132, 140, 152)
ACC = (30, 215, 96)
DIMLINE = (38, 42, 50)
ACCB = (122, 162, 210)

RUNDIR = os.environ.get("PI_DISPLAY_DIR", "/run/pi-display")
OVERRIDE = os.path.join(RUNDIR, "override.json")
NOWPLAYING = os.path.join(RUNDIR, "nowplaying.json")
COMMAND = os.path.join(RUNDIR, "command.json")


def geometry():
    try:
        w, h = open("/sys/class/graphics/fb0/virtual_size").read().strip().split(",")
        return int(w), int(h), int(open("/sys/class/graphics/fb0/bits_per_pixel").read().strip())
    except Exception:
        return 1024, 768, 16


W, H, BPP = geometry()
BAR_Y = H - 118
BAND_TOP = BAR_Y - 12
BAND_H = 62

_fonts = {}


def font(sz, bold=False):
    k = (sz, bold)
    if k not in _fonts:
        b = "/usr/share/fonts/truetype/dejavu/"
        try:
            _fonts[k] = ImageFont.truetype(
                b + ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"), sz
            )
        except Exception:
            _fonts[k] = ImageFont.load_default()
    return _fonts[k]


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def to565(img):
    a = np.asarray(img, dtype=np.uint16)
    v = ((a[:, :, 0] >> 3) << 11) | ((a[:, :, 1] >> 2) << 5) | (a[:, :, 2] >> 3)
    return v.astype("<u2").tobytes()


_on = None


def screen(on, force=False):
    global _on
    if not force and _on == on:
        return
    try:
        with open(BLANK, "w") as f:
            f.write("0" if on else "4")
        if on:
            os.system('printf "\\033[?25l\\033[2J" > /dev/tty1 2>/dev/null')
        _on = on
        log("display", "ON" if on else "OFF")
    except Exception as e:
        log("display switch failed:", e)


def blit_full(raw):
    with open(FB, "wb") as f:
        f.write(raw)


def blit_band(raw):
    with open(FB, "r+b") as f:
        f.seek(BAND_TOP * W * 2)
        f.write(raw)


S = {
    "mode": "idle",  # idle | spotify | paused | card | video
    "playing": False,
    "anchor_ms": 0,
    "anchor_t": 0.0,
    "dur": 0,
    "track": None,
    "base": None,
    "full_pending": False,
    "last_good": 0.0,
    "last_push": 0.0,
    "last_remote_push": 0.0,
    "source_updated_at_ms": 0,
    "render_generation": 0,
    "item": None,
    "last_video": None,
    "video_paused": False,
    "volume": 80,
}
LK = threading.Lock()
_CARD = {"sig": None, "generation": 0}
_VIDEO = {"proc": None}
_TOKEN = ""


class Spotify:
    def __init__(self, c):
        self.cid = c["client_id"]
        self.sec = c["client_secret"]
        self.rt = c["refresh_token"]
        self.tok = None
        self.exp = 0

    def _token(self):
        if self.tok and time.time() < self.exp - 90:
            return self.tok
        body = urllib.parse.urlencode(
            {"grant_type": "refresh_token", "refresh_token": self.rt}
        ).encode()
        auth = base64.b64encode((self.cid + ":" + self.sec).encode()).decode()
        r = urllib.request.Request(
            "https://accounts.spotify.com/api/token",
            data=body,
            headers={
                "Authorization": "Basic " + auth,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        d = json.loads(urllib.request.urlopen(r, timeout=8).read())
        self.tok = d["access_token"]
        self.exp = time.time() + d.get("expires_in", 3600)
        return self.tok

    def now(self):
        t0 = time.time()
        try:
            r = urllib.request.Request(
                "https://api.spotify.com/v1/me/player/currently-playing",
                headers={"Authorization": "Bearer " + self._token()},
            )
            resp = urllib.request.urlopen(r, timeout=6)
            rtt = time.time() - t0
            if resp.status == 204:
                return ("stopped", None, 0, 0, rtt)
            d = json.loads(resp.read() or b"{}")
            if d.get("is_playing") and d.get("item"):
                it = d["item"]
                return ("playing", it, d.get("progress_ms") or 0, it.get("duration_ms") or 0, rtt)
            if d.get("item"):
                it = d["item"]
                return ("paused", it, d.get("progress_ms") or 0, it.get("duration_ms") or 0, rtt)
            return ("stopped", None, 0, 0, rtt)
        except urllib.error.HTTPError as e:
            if e.code == 204:
                return ("stopped", None, 0, 0, time.time() - t0)
            if e.code == 401:
                self.tok = None
            if e.code == 429:
                try:
                    ra = float(e.headers.get("Retry-After", "5"))
                except Exception:
                    ra = 5.0
                return ("ratelimit", ra, 0, 0, time.time() - t0)
            return ("error", e, 0, 0, time.time() - t0)
        except Exception as e:
            return ("error", e, 0, 0, time.time() - t0)


_art = {}
_art_lock = threading.Lock()


def art(url, w, h):
    k = (url, w, h)
    with _art_lock:
        cached = _art.get(k)
    if cached is not None:
        return cached
    raw = urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": "pi-dash/1.0"}),
        timeout=3,
    ).read()
    image = Image.open(io.BytesIO(raw)).convert("RGB").resize((w, h), Image.LANCZOS)
    with _art_lock:
        _art.clear()
        _art[k] = image
    return image


def mmss(ms):
    s = max(0, int(ms // 1000))
    return "%d:%02d" % (s // 60, s % 60)


def base_frame(item, PA, fetch_art=True):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    AH = 400
    AW = int(round(AH * PA))
    ax = (W - AW) // 2
    ay = 46
    imgs = (item.get("album") or {}).get("images") or []
    poster = item.get("poster") or (imgs[0]["url"] if imgs else "")
    ok = False
    if poster and fetch_art:
        try:
            img.paste(art(poster, AW, AH), (ax, ay))
            ok = True
        except Exception as e:
            log("art error:", e)
    if not ok:
        d.rectangle([ax, ay, ax + AW, ay + AH], fill=(24, 26, 32))
    y = ay + AH + 34
    t = item.get("name") or item.get("track") or ""
    ft = font(38, True)
    while d.textlength(t, font=ft) > W - 120 and ft.size > 18:
        ft = font(ft.size - 2, True)
    d.text(((W - d.textlength(t, font=ft)) / 2, y), t, font=ft, fill=FGC)
    y += ft.size + 12
    artists = item.get("artists") or []
    if artists and isinstance(artists[0], dict):
        a = ", ".join(x.get("name", "") for x in artists)
    elif isinstance(artists, list):
        a = ", ".join(str(x) for x in artists)
    else:
        a = str(item.get("artist") or "")
    fa = font(25)
    while d.textlength(a, font=fa) > W - 140 and fa.size > 13:
        fa = font(fa.size - 2)
    d.text(((W - d.textlength(a, font=fa)) / 2, y), a, font=fa, fill=MUT)
    return img


def band(ms, dur):
    im = Image.new("RGB", (W, BAND_H), BG)
    d = ImageDraw.Draw(im)
    BW = 620
    bx = (W - BW) // 2
    ry = BAR_Y - BAND_TOP
    frac = max(0.0, min(1.0, ms / dur if dur else 0.0))
    d.rectangle([bx, ry, bx + BW, ry + 4], fill=DIMLINE)
    fill = BW * frac
    if fill >= 1:
        d.rectangle([bx, ry, bx + int(fill), ry + 4], fill=ACC)
        frac_px = fill - int(fill)
        if frac_px > 0.05:
            c = tuple(int(DIMLINE[i] + (ACC[i] - DIMLINE[i]) * frac_px) for i in range(3))
            d.rectangle([bx + int(fill), ry, bx + int(fill) + 1, ry + 4], fill=c)
    fs = font(15)
    d.text((bx, ry + 16), mmss(ms), font=fs, fill=MUT)
    tot = mmss(dur)
    d.text((bx + BW - d.textlength(tot, font=fs), ry + 16), tot, font=fs, fill=MUT)
    return im


def read_override():
    try:
        d = json.load(open(OVERRIDE))
    except Exception:
        return None
    if float(d.get("expires_at", 0)) < time.time():
        try:
            os.remove(OVERRIDE)
        except Exception:
            pass
        return None
    return d.get("payload") or None


def publish(playing, item, prog, dur):
    try:
        os.makedirs(RUNDIR, exist_ok=True)
        body = {"playing": bool(playing), "updated_at": time.time()}
        if item:
            artists = item.get("artists") or []
            if artists and isinstance(artists[0], dict):
                names = [a.get("name") for a in artists]
            else:
                names = [str(a) for a in artists]
            body.update({
                "track": item.get("name") or item.get("track"),
                "artists": names,
                "album": (item.get("album") or {}).get("name") if isinstance(item.get("album"), dict) else item.get("album"),
                "progress_ms": int(prog),
                "duration_ms": int(dur),
            })
        tmp = NOWPLAYING + ".tmp"
        with open(tmp, "w") as f:
            json.dump(body, f)
        os.replace(tmp, NOWPLAYING)
    except Exception:
        pass


def _wrap(d, text, fnt, maxw):
    words, lines, cur = (text or "").split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if d.textlength(t, font=fnt) <= maxw:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def render_card(p, poster_im=None):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((60, 44), (p.get("header") or "AGENT")[:60], font=font(18, True), fill=ACCB)
    d.line([(60, 76), (W - 60, 76)], fill=(38, 42, 50), width=1)
    PH = 420
    PW = int(round(PH * 0.67))
    px, py = 60, 108
    if poster_im is not None:
        img.paste(poster_im.resize((PW, PH), Image.LANCZOS), (px, py))
    else:
        d.rectangle([px, py, px + PW, py + PH], fill=(26, 29, 35))
    tx = px + PW + 46
    tw = W - tx - 60
    t = p.get("title") or ""
    ft = font(40, True)
    while d.textlength(t, font=ft) > tw and ft.size > 20:
        ft = font(ft.size - 2, True)
    d.text((tx, py + 2), t, font=ft, fill=FGC)
    y = py + 2 + ft.size + 14
    if p.get("subtitle"):
        d.text((tx, y), p["subtitle"][:70], font=font(21), fill=ACCB)
        y += 36
    for ln in _wrap(d, p.get("overview"), font(18), tw)[:4]:
        d.text((tx, y), ln, font=font(18), fill=MUT)
        y += 26
    y += 18
    for ln in (p.get("lines") or [])[:7]:
        if not isinstance(ln, str):
            ln = "%s: %s" % (ln.get("k") or ln.get("key") or "", ln.get("v") or ln.get("value") or "")
        if ":" in ln:
            k, v = ln.split(":", 1)
            d.text((tx, y), k.strip()[:18], font=font(18), fill=MUT)
            d.text((tx + 145, y), v.strip()[:44], font=font(18, True), fill=FGC)
        else:
            d.text((tx, y), ln[:60], font=font(18), fill=FGC)
        y += 29
    by = H - 96
    d.line([(60, by), (W - 60, by)], fill=(38, 42, 50), width=1)
    if p.get("prompt"):
        d.text((60, by + 26), p["prompt"][:70], font=font(24, True), fill=FGC)
    return img


def write_override(payload, ttl=180):
    os.makedirs(RUNDIR, exist_ok=True)
    body = {"expires_at": time.time() + max(5, min(int(ttl), 1800)), "payload": payload}
    fd, tmp = tempfile.mkstemp(prefix="ov-", suffix=".json", dir=RUNDIR)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(body, f)
        os.replace(tmp, OVERRIDE)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def apply_spotify(
    item,
    playing=True,
    progress_ms=0,
    duration_ms=0,
    PA=0.75,
    source_updated_at_ms=0,
):
    source_updated_at_ms = int(source_updated_at_ms or 0)
    with LK:
        if (
            source_updated_at_ms
            and source_updated_at_ms <= S["source_updated_at_ms"]
        ):
            log("ignored reordered spotify push", source_updated_at_ms)
            return
        if source_updated_at_ms:
            S["source_updated_at_ms"] = source_updated_at_ms
        _CARD["generation"] += 1
    stop_video()
    try:
        os.remove(OVERRIDE)
    except FileNotFoundError:
        pass
    dur = int(duration_ms or item.get("duration_ms") or 0)
    prog = int(progress_ms or 0)
    tid = item.get("id") or item.get("uri") or item.get("name")
    now = time.monotonic()
    with LK:
        new = tid != S["track"]
        S["mode"] = "spotify" if playing else "paused"
        S["playing"] = bool(playing)
        S["track"] = tid
        S["item"] = item
        S["dur"] = dur
        S["anchor_ms"] = prog
        S["anchor_t"] = now
        S["last_good"] = now
        S["last_push"] = now
        if source_updated_at_ms:
            S["last_remote_push"] = now
        needs_render = new or S["base"] is None
        if needs_render:
            S["render_generation"] += 1
            render_generation = S["render_generation"]
            S["base"] = None
            S["full_pending"] = False
        else:
            render_generation = S["render_generation"]
    publish(playing, item, prog, dur)

    def _commit_base(raw):
        with LK:
            if (
                S["track"] != tid
                or S["render_generation"] != render_generation
                or S["mode"] not in ("spotify", "paused")
            ):
                return False
            S["base"] = raw
            S["full_pending"] = True
            return True

    def _render():
        try:
            # Put fresh title/artist text on screen without waiting for the
            # album-art network request, then replace it with the art frame.
            if not _commit_base(to565(base_frame(item, PA, fetch_art=False))):
                return
            imgs = (item.get("album") or {}).get("images") or []
            poster = item.get("poster") or (imgs[0].get("url") if imgs else "")
            if poster:
                _commit_base(to565(base_frame(item, PA, fetch_art=True)))
        except Exception as e:
            log("render error:", e)

    if needs_render:
        threading.Thread(target=_render, daemon=True, name="spotify-art").start()
    log("now playing:" if playing else "paused:", (item.get("name") or "")[:48])


def apply_card(payload, ttl=180):
    stop_video()
    write_override(payload, ttl)
    with LK:
        S["mode"] = "card"
        S["last_push"] = time.monotonic()
        _CARD["generation"] += 1
        generation = _CARD["generation"]
    _CARD["sig"] = None
    screen(True)
    try:
        raw = to565(render_card(payload))
        with LK:
            if generation != _CARD["generation"] or S["mode"] != "card":
                return
            blit_full(raw)
            _CARD["sig"] = json.dumps(payload, sort_keys=True)
        log("card:", str(payload.get("title"))[:44])
    except Exception as e:
        log("card error:", e)

    poster = payload.get("poster")
    if not poster:
        return

    def _poster():
        try:
            raw = urllib.request.urlopen(
                urllib.request.Request(poster, headers={"User-Agent": "pi-dash/1.0"}),
                timeout=3,
            ).read()
            im = Image.open(io.BytesIO(raw)).convert("RGB")
            current = read_override()
            if (
                current is None
                or json.dumps(current, sort_keys=True) != json.dumps(payload, sort_keys=True)
            ):
                return
            rendered = to565(render_card(payload, im))
            with LK:
                if generation != _CARD["generation"] or S["mode"] != "card":
                    return
                blit_full(rendered)
        except Exception as e:
            log("card poster error:", e)

    threading.Thread(target=_poster, daemon=True).start()


def _safe_path(path: str) -> str | None:
    if not path:
        return None
    real = os.path.realpath(path)
    if not real.startswith("/data/"):
        return None
    if not os.path.isfile(real):
        return None
    return real


def stop_video():
    proc = _VIDEO.get("proc")
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    _VIDEO["proc"] = None


def _play_user() -> str:
    env = (os.environ.get("PI_DISPLAY_PLAY_USER") or "").strip()
    if env and env != "root":
        return env
    import pwd
    for candidate in ("/data/tv", "/data/movies", "/data"):
        try:
            name = pwd.getpwuid(os.stat(candidate).st_uid).pw_name
            if name and name != "root":
                return name
        except Exception:
            continue
    return ""


def _vlc_env(user: str) -> dict:
    env = os.environ.copy()
    if user:
        try:
            import pwd
            pw = pwd.getpwnam(user)
            env["HOME"] = pw.pw_dir
            env["USER"] = user
            env["LOGNAME"] = user
            rundir = f"/run/user/{pw.pw_uid}"
            env["XDG_RUNTIME_DIR"] = rundir if os.path.isdir(rundir) else "/tmp"
        except Exception:
            env.setdefault("XDG_RUNTIME_DIR", "/tmp")
    else:
        env.setdefault("XDG_RUNTIME_DIR", "/run/user/0")
    return env


def _hdmi_connector() -> str:
    explicit = (os.environ.get("PI_DISPLAY_HDMI") or "").strip()
    if explicit:
        return explicit
    try:
        for p in sorted(Path("/sys/class/drm").iterdir()):
            if "HDMI-A-" not in p.name:
                continue
            try:
                if (p / "status").read_text().strip() != "connected":
                    continue
            except Exception:
                continue
            n = p.name.rsplit("HDMI-A-", 1)[-1]
            if n.isdigit():
                return f"HDMI-{n}"
    except Exception:
        pass
    return "HDMI-2"


def _as_user(user: str, inner: list[str]) -> list[str]:
    if not (os.geteuid() == 0 and user):
        return inner
    env = _vlc_env(user)
    wrapped = ["env"]
    for key in ("HOME", "USER", "LOGNAME", "XDG_RUNTIME_DIR"):
        if env.get(key):
            wrapped.append("%s=%s" % (key, env[key]))
    # sudo env_reset drops Popen env; pass vars after `-- env`.
    return ["sudo", "-n", "-u", user, "--"] + wrapped + inner


def _vlc_cmds(path: str) -> tuple[list[list[str]], str]:
    user = _play_user()
    hdmi = _hdmi_connector()
    n = hdmi.rsplit("-", 1)[-1]
    alsa = "hw:%d,0" % max(0, int(n) - 1) if n.isdigit() else "hw:1,0"
    common = [
        "cvlc", "-I", "dummy", "--play-and-exit", "--no-video-title-show",
        "--fullscreen",
        "--extraintf=rc", "--rc-fake-tty",
        "--rc-host=%s:%d" % (VLC_RC_HOST, VLC_RC_PORT),
        "--vout=drm_vout",
        "--drm-vout-display=%s" % hdmi,
        "--drm-vout-window=fullscreen",
    ]
    attempts = [
        common + ["--aout=pulse", path],
        common + ["--aout=alsa", "--alsa-audio-device=%s" % alsa, path],
        common + ["--no-audio", path],
    ]
    return [_as_user(user, cmd) for cmd in attempts], user


def _video_alive() -> bool:
    proc = _VIDEO.get("proc")
    if proc and proc.poll() is None:
        return True
    try:
        return subprocess.call(
            ["pgrep", "-x", "vlc"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ) == 0
    except Exception:
        return False


def _signal_vlc(sig: int) -> bool:
    try:
        out = subprocess.check_output(["pgrep", "-x", "vlc"], text=True)
    except Exception:
        return False
    ok = False
    for line in out.split():
        try:
            os.kill(int(line), sig)
            ok = True
        except OSError:
            pass
    return ok


def _pactl_volume(percent: int | None = None, delta: int | None = None) -> bool:
    if percent is not None:
        args = ["set-sink-volume", "@DEFAULT_SINK@", "%d%%" % max(0, min(100, percent))]
    elif delta is not None:
        sign = "+" if int(delta) >= 0 else ""
        args = ["set-sink-volume", "@DEFAULT_SINK@", "%s%d%%" % (sign, int(delta))]
    else:
        return False
    cmd = _as_user(_play_user(), ["pactl"] + args)
    try:
        return subprocess.call(cmd, timeout=1.5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0
    except Exception:
        return False


def vlc_rc(line: str, timeout: float = 0.6) -> str:
    last = ""
    for _ in range(6):
        try:
            sock = socket.create_connection((VLC_RC_HOST, VLC_RC_PORT), timeout)
            sock.settimeout(timeout)
            try:
                try:
                    sock.recv(4096)
                except socket.timeout:
                    pass
                sock.sendall((line.strip() + "\n").encode())
                time.sleep(0.05)
                try:
                    last = sock.recv(4096).decode("utf-8", "replace")
                except socket.timeout:
                    last = ""
            finally:
                sock.close()
            return last
        except OSError:
            time.sleep(0.15)
    raise OSError("VLC RC not listening on %s:%d" % (VLC_RC_HOST, VLC_RC_PORT))


def apply_control(action: str, body: dict | None = None) -> bool:
    body = body or {}
    action = (action or "").lower().strip()
    if action in ("play", "unpause", "continue"):
        action = "resume"
    if not _video_alive():
        log("control ignored — no video:", action)
        return False
    with LK:
        paused = bool(S.get("video_paused"))
        vol = int(S.get("volume") or 80)
    try:
        if action == "pause":
            if not paused:
                try:
                    vlc_rc("pause")
                except OSError:
                    if not _signal_vlc(signal.SIGSTOP):
                        return False
                with LK:
                    S["video_paused"] = True
                    S["last_push"] = time.monotonic()
                log("video paused")
            return True
        if action == "resume":
            if paused:
                try:
                    vlc_rc("pause")
                except OSError:
                    if not _signal_vlc(signal.SIGCONT):
                        return False
                with LK:
                    S["video_paused"] = False
                    S["last_push"] = time.monotonic()
                log("video resumed")
            else:
                _signal_vlc(signal.SIGCONT)
            return True
        if action == "stop":
            stop_video()
            _signal_vlc(signal.SIGTERM)
            with LK:
                S["video_paused"] = False
                if S["mode"] == "video":
                    S["mode"] = "idle"
            return True
        if action == "volume":
            if body.get("percent") is not None:
                vol = max(0, min(100, int(body["percent"])))
            elif body.get("delta") is not None:
                vol = max(0, min(100, vol + int(body["delta"])))
            else:
                return False
            try:
                vlc_rc("volume %d" % int(round(vol * 2.56)))
            except OSError:
                if not _pactl_volume(percent=vol):
                    return False
            with LK:
                S["volume"] = vol
                S["last_push"] = time.monotonic()
            log("video volume", vol)
            return True
    except Exception as e:
        log("vlc rc error:", e)
        return False
    return False


def _fb_blank() -> str:
    try:
        return open(BLANK).read().strip()
    except Exception:
        return ""


def _status(full: bool = False) -> dict:
    proc = _VIDEO.get("proc")
    now = time.monotonic()
    with LK:
        mode = S["mode"]
        last = dict(S.get("last_video") or {}) if S.get("last_video") else None
        item = dict(S.get("item") or {}) if S.get("item") else None
        playing = bool(S.get("playing"))
        progress_ms = int(S.get("anchor_ms") or 0)
        if playing:
            progress_ms += int(max(0.0, now - S.get("anchor_t", now)) * 1000)
        duration_ms = int(S.get("dur") or 0)
        if duration_ms:
            progress_ms = min(progress_ms, duration_ms)
        remote_age = (
            max(0.0, now - S["last_remote_push"])
            if S.get("last_remote_push")
            else None
        )
    blank = _fb_blank()
    body = {
        "ok": True,
        "mode": mode,
        "blank": blank,
        "screen_on": blank in ("0", ""),
        "video_alive": bool(proc and proc.poll() is None),
        "video_paused": bool(S.get("video_paused")),
        "volume": int(S.get("volume") or 80),
        "spotify_playing": playing,
        "spotify_track": (item or {}).get("name") or (item or {}).get("track") or "",
        "spotify_progress_ms": progress_ms,
        "spotify_duration_ms": duration_ms,
        "last_remote_push_age_seconds": round(remote_age, 2) if remote_age is not None else None,
    }
    if last:
        if full:
            body["last_video"] = last
        else:
            body["last_rc"] = last.get("rc")
            body["last_error"] = last.get("error") or ""
    return body


def apply_video(path: str, title: str = ""):
    real = _safe_path(path)
    if not real:
        log("video rejected:", (path or "")[:80])
        with LK:
            S["last_video"] = {
                "ok": False,
                "path": (path or "")[:120],
                "title": title,
                "error": "path rejected",
                "at": time.time(),
            }
        return False
    stop_video()
    try:
        os.remove(OVERRIDE)
    except FileNotFoundError:
        pass
    with LK:
        S["mode"] = "video"
        S["playing"] = False
        S["last_push"] = time.monotonic()
        _CARD["generation"] += 1
        S["last_video"] = {
            "ok": None,
            "path": real,
            "title": title,
            "error": "queued",
            "at": time.time(),
        }
        S["video_paused"] = False
    screen(True, force=True)
    log("video queued:", title or real)

    def _run():
        global _on
        attempts, user = _vlc_cmds(real)
        logf = os.path.join(RUNDIR, "vlc.log")
        last_rc = None
        last_err = ""
        ok = False
        try:
            open(logf, "w").close()
        except Exception:
            pass
        try:
            for cmd in attempts:
                log("vlc as %s:" % (user or "self"), " ".join(cmd[:-1]), os.path.basename(real))
                with open(logf, "ab") as err:
                    err.write(("\n$ " + " ".join(cmd) + "\n").encode())
                    proc = subprocess.Popen(cmd, stdout=err, stderr=err)
                    _VIDEO["proc"] = proc
                    last_rc = proc.wait()
                log("vlc exit", last_rc)
                if last_rc == 0:
                    ok = True
                    break
            if not ok:
                try:
                    tail = open(logf, "rb").read()[-800:].decode("utf-8", "replace")
                    last_err = tail.strip().splitlines()[-1] if tail.strip() else f"exit {last_rc}"
                except Exception:
                    last_err = f"exit {last_rc}"
        except Exception as e:
            last_err = str(e)
            log("vlc error:", e)
        _VIDEO["proc"] = None
        _on = None  # VLC drm_vout often leaves fb blanked; don't trust cached state.
        with LK:
            S["last_video"] = {
                "ok": ok,
                "path": real,
                "title": title,
                "user": user,
                "rc": last_rc,
                "error": "" if ok else last_err,
                "at": time.time(),
            }
        if ok:
            with LK:
                if S["mode"] == "video":
                    S["mode"] = "idle"
            return
        hint = ""
        low = real.lower()
        if "x265" in low or "hevc" in low:
            hint = "This file is HEVC/x265 — Pi 4 may not play it."
        apply_card(
            {
                "header": "DISPLAY",
                "title": title or os.path.basename(real),
                "subtitle": "Video did not start",
                "overview": (hint or last_err or "VLC exited")[:400],
                "lines": [
                    f"exit: {last_rc}",
                    f"user: {user or 'self'}",
                ],
                "prompt": "HDMI is on — check /status",
            },
            ttl=180,
        )

    threading.Thread(target=_run, daemon=True).start()
    return True


def load_token():
    global _TOKEN
    try:
        _TOKEN = open(TOKEN_FILE).read().strip()
    except Exception:
        try:
            _TOKEN = str(json.load(open(CONF)).get("display_token") or "")
        except Exception:
            _TOKEN = ""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _auth(self) -> bool:
        h = self.headers.get("Authorization") or ""
        if _TOKEN and h == "Bearer " + _TOKEN:
            return True
        self.send_response(401)
        self.end_headers()
        return False

    def do_GET(self):
        if self.path in ("/health", "/"):
            body = json.dumps(_status(full=False)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/status":
            if not self._auth():
                return
            body = json.dumps(_status(full=True)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if not self._auth():
            return
        n = int(self.headers.get("Content-Length") or 0)
        if n > 200_000:
            self.send_response(413)
            self.end_headers()
            return
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            self.send_response(400)
            self.end_headers()
            return
        try:
            ok = handle_push(body)
        except Exception as e:
            log("push error:", e)
            ok = False
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}' if ok else b'{"ok":false}')


def handle_push(body: dict) -> bool:
    kind = (body.get("type") or body.get("kind") or "spotify").lower()
    PA = float(S.get("PA") or 0.75)
    if kind in ("spotify", "now_playing", "nowplaying"):
        item = body.get("item") or {
            "id": body.get("id"),
            "name": body.get("track") or body.get("name") or "",
            "artists": body.get("artists") or [{"name": body.get("artist") or ""}],
            "album": {"images": [{"url": body.get("poster")}] if body.get("poster") else []},
            "poster": body.get("poster"),
            "duration_ms": body.get("duration_ms") or 0,
        }
        if body.get("poster") and not (item.get("album") or {}).get("images"):
            item["poster"] = body["poster"]
        apply_spotify(
            item,
            playing=body.get("playing", True),
            progress_ms=body.get("progress_ms") or 0,
            duration_ms=body.get("duration_ms") or item.get("duration_ms") or 0,
            PA=PA,
            source_updated_at_ms=body.get("source_updated_at_ms") or 0,
        )
        return True
    if kind == "card":
        apply_card(body.get("payload") or body, ttl=int(body.get("ttl_seconds") or body.get("ttl") or 180))
        return True
    if kind in ("video", "play"):
        return apply_video(body.get("path") or "", body.get("title") or "")
    if kind in ("control", "pause", "resume", "stop", "volume"):
        action = kind if kind != "control" else (body.get("action") or "pause")
        return apply_control(action, body)
    if kind in ("clear", "idle", "off"):
        stop_video()
        try:
            os.remove(OVERRIDE)
        except FileNotFoundError:
            pass
        with LK:
            S["mode"] = "idle"
            S["playing"] = False
            S["track"] = None
            S["base"] = None
            _CARD["generation"] += 1
        return True
    return False


def consume_command_file():
    try:
        d = json.load(open(COMMAND))
        os.remove(COMMAND)
    except Exception:
        return
    handle_push(d)


def poller(sp, PA, GRACE):
    while True:
        consume_command_file()
        with LK:
            fresh_for = time.monotonic() - S["last_remote_push"]
            fresh = fresh_for < PUSH_FRESH
            video = S["mode"] == "video"
        if video:
            time.sleep(0.25)
            continue
        if read_override() is not None:
            time.sleep(0.25)
            continue
        if fresh:
            time.sleep(min(0.25, max(0.05, PUSH_FRESH - fresh_for)))
            continue
        state, item, p, dur, _rtt = sp.now()
        now = time.monotonic()
        if state == "playing" and item:
            # Anchor at receipt. Adding half the HTTP RTT made the display lead
            # real playback, especially while the Spotify client was buffering.
            apply_spotify(item, True, p, dur, PA)
        elif state == "paused" and item:
            apply_spotify(item, False, p, dur, PA)
        elif state == "stopped":
            with LK:
                if now - S["last_good"] > GRACE:
                    S["playing"] = False
                    S["mode"] = "idle"
            publish(False, None, 0, 0)
        elif state == "ratelimit":
            nap = min(120.0, max(5.0, float(item) + 0.5))
            log("spotify fallback poll 429, backing off %.0fs" % nap)
            time.sleep(nap)
            continue
        else:
            log("api hiccup (holding):", str(item)[:60])
        time.sleep(POLL_PLAYING if state == "playing" else POLL_IDLE)


def start_http():
    load_token()
    if not _TOKEN:
        log("display HTTP disabled — no token")
        return
    httpd = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler)
    log("display HTTP :%d" % HTTP_PORT)
    httpd.serve_forever()


def main():
    conf = json.load(open(CONF))
    PA = float(conf.get("pixel_aspect", 0.75))
    GRACE = float(conf.get("grace_seconds", 25))
    S["PA"] = PA
    os.makedirs(RUNDIR, exist_ok=True)
    log("geometry %dx%d@%d  pixel_aspect=%.3f  fps=%.0f  poll=%.1fs  push-http=:%d" % (
        W, H, BPP, PA, FPS, POLL_PLAYING, HTTP_PORT,
    ))
    threading.Thread(target=start_http, daemon=True).start()
    sp = Spotify(conf)
    threading.Thread(target=poller, args=(sp, PA, GRACE), daemon=True).start()
    period = 1.0 / FPS
    while True:
        t = time.monotonic()
        consume_command_file()
        with LK:
            mode = S["mode"]
            playing = S["playing"]
            base = S["base"]
            dur = S["dur"]
            am, at = S["anchor_ms"], S["anchor_t"]
            pending = S["full_pending"]
            if pending:
                S["full_pending"] = False
        if mode == "video":
            time.sleep(period)
            continue
        ov = read_override()
        if ov is not None:
            screen(True)
            sig = json.dumps(ov, sort_keys=True)
            if sig != _CARD["sig"]:
                try:
                    blit_full(to565(render_card(ov)))
                    _CARD["sig"] = sig
                    log("card:", str(ov.get("title"))[:44])
                except Exception as e:
                    log("card error:", e)
            time.sleep(period)
            continue
        if _CARD["sig"] is not None:
            _CARD["sig"] = None
            with LK:
                S["full_pending"] = True
        if (playing or mode == "paused") and base is not None:
            screen(True)
            if pending:
                try:
                    blit_full(base)
                except Exception as e:
                    log("full blit error:", e)
            est = am if mode == "paused" else am + (t - at) * 1000.0
            if dur:
                est = min(est, dur)
            try:
                blit_band(to565(band(est, dur)))
            except Exception as e:
                log("band error:", e)
        elif mode == "idle" and not playing:
            screen(False)
            with LK:
                S["track"] = None
                S["base"] = None
        dt = time.monotonic() - t
        time.sleep(max(0.0, period - dt))


if __name__ == "__main__":
    main()
