"""
ULTRON — drop-in agent skills.

Each skills/<name>/SKILL.md is loaded at startup. Matching skills are
injected into the live system block for that turn only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .paths import SKILLS as SKILLS_DIR

_WRAP = (
    "This skill applies to written email/compose bodies only. "
    "Spoken replies stay in character: one sentence, do not read the email aloud. "
    "Never use em dashes in email text.\n\n"
)


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str


_CACHE: list[Skill] | None = None

# Tokens in a skill name that map to an intent family.
_FAMILY_ALIASES = {
    "gmail": ("email", "gmail", "mail", "reply"),
}


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    raw = text[3:end].strip()
    body = text[end + 4:].strip()
    meta: dict[str, str] = {}
    key: str | None = None
    acc: list[str] = []
    for line in raw.splitlines():
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if m and not line.startswith(" "):
            if key:
                meta[key] = " ".join(x for x in acc if x).strip()
            key = m.group(1)
            rest = m.group(2).strip().lstrip(">|").strip()
            acc = [rest] if rest else []
        elif key:
            acc.append(line.strip())
    if key:
        meta[key] = " ".join(x for x in acc if x).strip()
    return meta, body


def load_skills() -> list[Skill]:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    skills: list[Skill] = []
    if not SKILLS_DIR.is_dir():
        _CACHE = skills
        return skills
    for path in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        meta, body = _frontmatter(text)
        skills.append(Skill(
            name=meta.get("name") or path.parent.name,
            description=meta.get("description", ""),
            body=body,
        ))
    _CACHE = skills
    return skills


def _applies(
    skill: Skill,
    transcript: str,
    family: str | None,
    tool: str | None,
) -> bool:
    t = transcript.lower()
    hay = f"{skill.name} {skill.description}".lower()
    tokens = [p for p in skill.name.split("-") if len(p) > 3]
    if skill.name == "email-reply-drafter" and tool in ("search_gmail", "read_email"):
        return False
    if family:
        aliases = _FAMILY_ALIASES.get(family, (family,))
        if any(a in hay or a in skill.name for a in aliases):
            if skill.name != "email-reply-drafter" or tool in (
                "draft_gmail", "send_gmail", None,
            ):
                return True
    return any(tok in t for tok in tokens)


def block_for(transcript: str, family: str | None, tool: str | None = None) -> str:
    """Markdown to append to the live system block, or empty."""
    matched = [s for s in load_skills() if _applies(s, transcript, family, tool)]
    if not matched:
        return ""
    parts = []
    for s in matched:
        print(f"\033[92m[BRAIN] Skill: {s.name}\033[0m", flush=True)
        parts.append(f"## Skill: {s.name}\n\n{_WRAP}{s.body}")
    return "\n\n".join(parts)
