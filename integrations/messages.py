"""
J.A.R.V.I.S. — Messages Integration
Send iMessages via AppleScript. Query the local Messages database (chat.db).
Looks up contacts by name automatically.
AirDrop: opens share sheet for a file path (recipient selection is still visual on macOS).
"""

import re
import shutil
import sqlite3
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
RESET  = "\033[0m"


# ── Contact lookup ─────────────────────────────────────────────────────────────

def _lookup_contact(name: str) -> tuple[str | None, str | None]:
    """
    Look up a contact by name in macOS Contacts.app via AppleScript.
    Returns (full_name, phone_or_email) or (None, None) if not found.
    Tries phone first, falls back to email for iMessage.
    """
    script = f'''
    tell application "Contacts"
        set matchedPeople to (people whose name contains "{name}")
        if length of matchedPeople is 0 then
            return "NOT_FOUND"
        end if
        set thePerson to item 1 of matchedPeople
        set personName to name of thePerson
        -- Try phone first
        if (count of phones of thePerson) > 0 then
            set thePhone to value of item 1 of phones of thePerson
            return personName & "|" & thePhone
        end if
        -- Fall back to email
        if (count of emails of thePerson) > 0 then
            set theEmail to value of item 1 of emails of thePerson
            return personName & "|" & theEmail
        end if
        return "NO_CONTACT_INFO"
    end tell
    '''
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True
    )
    output = result.stdout.strip()

    if output in ("NOT_FOUND", "NO_CONTACT_INFO", ""):
        return None, None

    parts = output.split("|", 1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return None, None


def _clean_phone(phone: str) -> str:
    """Strip formatting from phone numbers, keep + prefix."""
    digits = re.sub(r"[^\d+]", "", phone)
    return digits


# ── iMessage ───────────────────────────────────────────────────────────────────

def send_imessage(to: str, message: str) -> str:
    """
    Send an iMessage to a contact name, phone number, or email.
    If a name is given, looks up the contact in Contacts.app first.
    """
    recipient = to.strip()
    display_name = to.strip()

    # If it looks like a name (not a number or email), look it up
    is_phone  = bool(re.match(r"^[\d\s\(\)\-\+]+$", recipient))
    is_email  = "@" in recipient

    if not is_phone and not is_email:
        full_name, contact_info = _lookup_contact(recipient)
        if contact_info is None:
            return (
                f"I couldn't find '{to}' in your contacts, sir. "
                f"Try using their phone number or email directly."
            )
        display_name = full_name or to
        recipient    = _clean_phone(contact_info) if "@" not in contact_info else contact_info
        print(f"{GREEN}[MESSAGES] Resolved '{to}' → {display_name} ({recipient}){RESET}", flush=True)
    else:
        if is_phone:
            recipient = _clean_phone(recipient)

    # Pass recipient + message as argv — avoids ALL AppleScript escaping issues
    # (apostrophes, quotes, special chars all work fine this way)
    script = '''
    on run argv
        set theRecipient to item 1 of argv
        set theMessage to item 2 of argv
        tell application "Messages"
            set targetService to 1st service whose service type = iMessage
            set targetBuddy to buddy theRecipient of targetService
            send theMessage to targetBuddy
        end tell
    end run
    '''
    result = subprocess.run(
        ["osascript", "-e", script, recipient, message],
        capture_output=True, text=True
    )

    if result.returncode != 0:
        err = result.stderr.strip()
        # If iMessage buddy not found, try SMS fallback
        if "buddy" in err.lower() or "not found" in err.lower():
            script_sms = '''
            on run argv
                set theRecipient to item 1 of argv
                set theMessage to item 2 of argv
                tell application "Messages"
                    set smsServices to services where service type = SMS
                    if length of smsServices > 0 then
                        send theMessage to buddy theRecipient of item 1 of smsServices
                    end if
                end tell
            end run
            '''
            result2 = subprocess.run(
                ["osascript", "-e", script_sms, recipient, message],
                capture_output=True, text=True
            )
            if result2.returncode != 0:
                print(f"{RED}[MESSAGES] Send failed: {result2.stderr.strip()}{RESET}", flush=True)
                return (
                    f"Couldn't reach {display_name} via iMessage or SMS, sir. "
                    f"Make sure Messages.app is open and they're in your contacts."
                )
        else:
            print(f"{RED}[MESSAGES] AppleScript error: {err}{RESET}", flush=True)
            return f"Message to {display_name} failed, sir: {err}"

    print(f"{GREEN}[MESSAGES] Sent iMessage to {display_name} ({recipient}){RESET}", flush=True)
    return f"Message sent to {display_name}, sir."


# ── AirDrop ────────────────────────────────────────────────────────────────────

def airdrop_file(file_path: str) -> str:
    """
    Open the macOS share sheet for a file so it can be AirDropped.
    Recipient selection is still visual — this just surfaces the share sheet.
    """
    import os
    if not os.path.exists(file_path):
        return f"I can't find that file at '{file_path}', sir."

    # Use macOS sharing picker via osascript + share:
    script = f'''
    tell application "Finder"
        activate
        set theFile to POSIX file "{file_path}" as alias
        share theFile
    end tell
    '''
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        # Fallback: open Finder to the file so user can right-click share
        subprocess.run(["open", "-R", file_path])
        return (
            f"Opened Finder to the file, sir — right-click it and choose Share → AirDrop "
            f"to send it. Full AirDrop automation isn't available on macOS."
        )

    print(f"{GREEN}[MESSAGES] AirDrop share sheet opened for {file_path}{RESET}", flush=True)
    return (
        f"Share sheet is open, sir — select the AirDrop recipient from the menu "
        f"to complete the transfer."
    )


# ── Query local Messages database ──────────────────────────────────────────────

CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
_FDA_HINT = (
    "I can't read your Messages database, sir. Grant Full Disk Access to Jarvis "
    "(or Terminal, if you're running from there) in System Settings → Privacy & "
    "Security → Full Disk Access, then try again."
)

def _digits(value: str) -> str:
    d = re.sub(r"\D", "", value or "")
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d


def _norm_handle(value: str) -> str:
    raw = (value or "").strip().lower()
    if "@" in raw:
        return raw
    digits = _digits(raw)
    return digits or raw


def _handle_matches(handle: str, idents: list[str]) -> bool:
    if not handle or not idents:
        return False
    h = _norm_handle(handle)
    hd = _digits(handle)
    for ident in idents:
        i = _norm_handle(ident)
        if i and (i in h or h in i):
            return True
        idigits = _digits(ident)
        if hd and idigits and hd == idigits:
            return True
    return False


def _contact_identifiers(contact: str) -> list[str]:
    """Resolve a name/phone/email to identifiers that appear in chat.db."""
    raw = contact.strip()
    is_phone = bool(re.match(r"^[\d\s\(\)\-\+]+$", raw))
    is_email = "@" in raw
    idents: list[str] = []

    if is_phone:
        cleaned = _clean_phone(raw)
        idents.append(cleaned)
        digits = _digits(raw)
        if digits:
            idents.append(digits)
            idents.append("+1" + digits)
    elif is_email:
        idents.append(raw.lower())
    else:
        full_name, info = _lookup_contact(raw)
        if info:
            if "@" in info:
                idents.append(info.lower())
            else:
                cleaned = _clean_phone(info)
                idents.append(cleaned)
                digits = _digits(info)
                if digits:
                    idents.append(digits)
                    idents.append("+1" + digits)
        # Always include the raw name — group chats use display_name.
        idents.append(raw)
        if full_name and full_name.lower() != raw.lower():
            idents.append(full_name)

    # Dedupe, keep order
    seen: set[str] = set()
    out: list[str] = []
    for i in idents:
        key = i.lower()
        if key and key not in seen:
            seen.add(key)
            out.append(i)
    return out


def _decode_attributed_body(blob) -> str:
    """Pull plain text out of an NSKeyedArchiver / typedstream blob."""
    if not blob:
        return ""
    data = bytes(blob)
    marker = data.find(b"NSString")
    if marker < 0:
        return ""
    payload = data[marker + 8:]
    best = ""
    i = 0
    n = len(payload)
    while i < n:
        length = payload[i]
        end = i + 1 + length
        if 2 <= length <= 220 and end <= n:
            chunk = payload[i + 1:end]
            try:
                s = chunk.decode("utf-8")
            except UnicodeDecodeError:
                i += 1
                continue
            if _looks_like_message(s) and len(s) > len(best):
                best = s
        i += 1
    if best:
        return best.strip()

    # Fallback: printable run after NSString
    text = payload.decode("utf-8", errors="ignore")
    for stop in ("NSDictionary", "NSNumber", "NSMutableAttributedString"):
        if stop in text:
            text = text.split(stop, 1)[0]
    cleaned = "".join(
        ch if ch in "\n\t" or ch.isprintable() else " "
        for ch in text
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" \t\n\r+").strip()
    return cleaned if _looks_like_message(cleaned) else ""


def _looks_like_message(s: str) -> bool:
    if not s or len(s) < 1:
        return False
    if s in {
        "NSString", "NSDictionary", "NSAttributedString",
        "NSNumber", "NSMutableString", "NSMutableAttributedString",
        "NSValue", "NSData",
    }:
        return False
    printable = sum(1 for c in s if c.isprintable() or c in "\n\t")
    return printable / max(len(s), 1) > 0.85


def _message_text(row) -> str:
    text = (row["text"] or "").strip()
    if text and text != "\ufffc":
        return text
    decoded = _decode_attributed_body(row["attributedBody"])
    if decoded and decoded != "\ufffc":
        return decoded
    if row["cache_has_attachments"]:
        return "[attachment]"
    return ""


def _apple_ns(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return int((dt.astimezone(timezone.utc) - APPLE_EPOCH).total_seconds() * 1e9)


def _from_apple_time(ts) -> datetime | None:
    if ts is None:
        return None
    try:
        val = int(ts)
    except (TypeError, ValueError):
        return None
    # Catalina+ stores nanoseconds; older DBs used seconds.
    if abs(val) > 1_000_000_000_000:
        seconds = val / 1e9
    else:
        seconds = val
    return APPLE_EPOCH + timedelta(seconds=seconds)


def _parse_since(since: str | None) -> datetime | None:
    if not since:
        return None
    raw = since.strip().lower()
    now = datetime.now().astimezone()
    start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if raw in ("today",):
        return start_today
    if raw in ("yesterday",):
        return start_today - timedelta(days=1)
    if raw in ("this week",):
        return start_today - timedelta(days=now.weekday())
    if raw in ("last week",):
        return start_today - timedelta(days=now.weekday() + 7)
    m = re.match(r"last\s+(\d+)\s+days?", raw)
    if m:
        return start_today - timedelta(days=int(m.group(1)))
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y"):
        try:
            parsed = datetime.strptime(raw, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=now.tzinfo)
            return parsed
        except ValueError:
            continue
    return None


def _fmt_when(dt: datetime | None) -> str:
    if dt is None:
        return "unknown time"
    local = dt.astimezone()
    now = datetime.now().astimezone()
    if local.date() == now.date():
        return local.strftime("%-I:%M %p")
    if local.date() == (now - timedelta(days=1)).date():
        return local.strftime("yesterday %-I:%M %p")
    if (now - local).days < 7:
        return local.strftime("%a %-I:%M %p")
    return local.strftime("%b %-d %-I:%M %p")


@contextmanager
def _chat_connection():
    """Copy chat.db (+ WAL) so we can read while Messages.app has it open."""
    src = CHAT_DB
    if not src.exists():
        raise FileNotFoundError(str(src))

    tmpdir = tempfile.mkdtemp(prefix="jarvis-chatdb-")
    dest = Path(tmpdir) / "chat.db"
    con = None
    try:
        shutil.copy2(src, dest)
        for suffix in ("-wal", "-shm"):
            extra = Path(str(src) + suffix)
            if extra.exists():
                shutil.copy2(extra, Path(str(dest) + suffix))
        con = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        yield con
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
        shutil.rmtree(tmpdir, ignore_errors=True)


def search_imessage(
    contact: str = "",
    query: str = "",
    since: str = "",
    max_results: int = 15,
) -> str:
    """
    Search the local iMessage/SMS database.
    contact: name, phone, or email (optional)
    query: text to find in message bodies (optional)
    since: today, yesterday, last N days, or YYYY-MM-DD (optional)
    """
    try:
        return _search_imessage(
            contact=contact or "",
            query=query or "",
            since=since or "",
            max_results=max_results or 15,
        )
    except PermissionError:
        print(f"{RED}[MESSAGES] Full Disk Access denied for chat.db{RESET}", flush=True)
        return _FDA_HINT
    except OSError as exc:
        print(f"{RED}[MESSAGES] chat.db I/O error: {exc}{RESET}", flush=True)
        if getattr(exc, "errno", None) in (1, 13) or "not permitted" in str(exc).lower():
            return _FDA_HINT
        return f"Couldn't query your texts, sir: {exc}"
    except FileNotFoundError:
        return "I can't find your Messages database, sir. Is Messages.app set up on this Mac?"
    except sqlite3.OperationalError as exc:
        err = str(exc).lower()
        if "authorization" in err or "unable to open" in err or "permission" in err:
            print(f"{RED}[MESSAGES] chat.db blocked: {exc}{RESET}", flush=True)
            return _FDA_HINT
        print(f"{RED}[MESSAGES] SQL error: {exc}{RESET}", flush=True)
        return f"Couldn't query your texts, sir: {exc}"
    except Exception as exc:
        print(f"{RED}[MESSAGES] Search failed: {exc}{RESET}", flush=True)
        return f"Couldn't query your texts, sir: {exc}"


def _search_imessage(contact: str, query: str, since: str, max_results: int) -> str:
    max_results = max(1, min(int(max_results), 40))
    needle = query.strip().lower()
    cutoff = _parse_since(since)
    idents = _contact_identifiers(contact) if contact.strip() else []

    clauses = [
        "(m.associated_message_type IS NULL OR m.associated_message_type = 0)",
        "IFNULL(m.item_type, 0) = 0",
    ]
    params: list = []

    if cutoff is not None:
        clauses.append("m.date >= ?")
        params.append(_apple_ns(cutoff))

    if idents:
        contact_bits = []
        for ident in idents:
            contact_bits.append("IFNULL(h.id, '') LIKE ?")
            params.append(f"%{ident}%")
            contact_bits.append("IFNULL(c.chat_identifier, '') LIKE ?")
            params.append(f"%{ident}%")
            contact_bits.append("IFNULL(c.display_name, '') LIKE ?")
            params.append(f"%{ident}%")
        clauses.append("(" + " OR ".join(contact_bits) + ")")

    where = " AND ".join(clauses)
    # Pull a window so we can decode attributedBody and filter in Python.
    fetch_limit = 2000 if needle else max(80, max_results * 8)
    params.append(fetch_limit)

    def _sql(where_sql: str) -> str:
        return f"""
            SELECT
                m.ROWID AS id,
                m.text,
                m.attributedBody,
                m.is_from_me,
                m.date,
                m.service,
                m.cache_has_attachments,
                h.id AS handle,
                c.display_name AS chat_name,
                c.chat_identifier AS chat_id
            FROM message m
            LEFT JOIN handle h ON h.ROWID = m.handle_id
            LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
            LEFT JOIN chat c ON c.ROWID = cmj.chat_id
            WHERE {where_sql}
            ORDER BY m.date DESC
            LIMIT ?
        """

    with _chat_connection() as con:
        try:
            rows = con.execute(_sql(where), params).fetchall()
        except sqlite3.OperationalError as exc:
            if "item_type" not in str(exc):
                raise
            clauses.remove("IFNULL(m.item_type, 0) = 0")
            rows = con.execute(_sql(" AND ".join(clauses)), params).fetchall()

    seen: set[int] = set()
    hits: list[str] = []
    for row in rows:
        rid = int(row["id"])
        if rid in seen:
            continue
        seen.add(rid)
        body = _message_text(row)
        if not body:
            continue
        if needle and needle not in body.lower():
            continue

        when = _fmt_when(_from_apple_time(row["date"]))
        handle = row["handle"] or ""
        chat_name = row["chat_name"] or ""
        other = (
            contact.strip()
            if contact.strip() and _handle_matches(handle, idents)
            else (handle or "Unknown")
        )
        if row["is_from_me"]:
            if chat_name:
                speaker = f"You in {chat_name}"
            elif other not in ("", "Unknown"):
                speaker = f"You → {other}"
            else:
                speaker = "You"
        elif chat_name and chat_name != other:
            speaker = f"{other} in {chat_name}"
        else:
            speaker = other

        if len(body) > 400:
            body = body[:397] + "..."
        hits.append(f"[{when}] {speaker}: {body}")
        if len(hits) >= max_results:
            break

    label_bits = []
    if contact.strip():
        label_bits.append(f"with {contact.strip()}")
    if query.strip():
        label_bits.append(f"matching '{query.strip()}'")
    if since.strip():
        label_bits.append(f"since {since.strip()}")
    scope = " ".join(label_bits) if label_bits else "recent"

    if not hits:
        print(f"{YELLOW}[MESSAGES] No texts {scope}{RESET}", flush=True)
        return f"No texts found {scope}, sir."

    # Chronological for the spoken summary
    hits.reverse()
    print(f"{GREEN}[MESSAGES] {len(hits)} texts {scope}{RESET}", flush=True)
    return f"{len(hits)} texts {scope}:\n\n" + "\n".join(hits)
