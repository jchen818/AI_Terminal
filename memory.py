"""Chat history on disk, and the memory of past sessions given to the model.

Every conversation is saved as one JSON file under <config dir>/chats. A new
chat starts with a short digest of recent ones in the system prompt — what was
asked, which commands ran and whether they worked, how it ended — plus a
notes file of standing facts. The assistant adds to the notes by writing a
line that starts with REMEMBER:, and the user can edit them in the Memory
dialog.
"""

import json
import re
import time
from pathlib import Path

from config import config_dir

REMEMBER_RE = re.compile(r"^[ \t>*_-]*REMEMBER:[ \t]*(.+?)[ \t*_]*$", re.M)
DONE_RE = re.compile(r"^\W*DONE\b", re.M)
CODE_BLOCK = re.compile(r"```.*?```", re.DOTALL)

MEMORY_CHARS = 8000       # most characters of memory added to the prompt
COMMANDS_PER_CHAT = 15    # commands listed per past chat
OUTCOME_CHARS = 400       # characters of each chat's ending shown


def chats_dir() -> Path:
    d = config_dir() / "chats"
    d.mkdir(parents=True, exist_ok=True)
    return d


def notes_path() -> Path:
    return config_dir() / "memory_notes.md"


def new_chat_id() -> str:
    base = time.strftime("%Y%m%d-%H%M%S")
    cid, n = base, 1
    while (chats_dir() / f"{cid}.json").exists():
        n += 1
        cid = f"{base}-{n}"
    return cid


def title_from(text: str) -> str:
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return (line[:60] + "…") if len(line) > 60 else (line or "Untitled chat")


# ------------------------------------------------------------- chat files

def save_chat(chat: dict) -> None:
    """Write one chat. Written to a temp file first so a crash cannot leave a
    half-written file behind."""
    path = chats_dir() / f"{chat['id']}.json"
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(chat, fh, ensure_ascii=False, indent=1)
    tmp.replace(path)


def load_chat(chat_id: str):
    try:
        with open(chats_dir() / f"{chat_id}.json", "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def delete_chat(chat_id: str) -> None:
    try:
        (chats_dir() / f"{chat_id}.json").unlink()
    except OSError:
        pass


def list_chats(limit: int = 30) -> list:
    """Newest first: [{id, title, updated}] without loading whole files twice."""
    out = []
    for path in sorted(chats_dir().glob("*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        out.append({"id": data.get("id", path.stem),
                    "title": data.get("title", "Untitled chat"),
                    "updated": data.get("updated", path.stat().st_mtime),
                    "data": data})
    return out


def clear_history() -> int:
    n = 0
    for path in chats_dir().glob("*.json"):
        try:
            path.unlink()
            n += 1
        except OSError:
            pass
    return n


# ------------------------------------------------------------------ notes

def load_notes() -> str:
    try:
        return notes_path().read_text(encoding="utf-8")
    except OSError:
        return ""


def save_notes(text: str) -> None:
    notes_path().write_text(text.strip() + "\n" if text.strip() else "",
                            encoding="utf-8")


def add_notes(reply: str) -> list:
    """Save REMEMBER: lines from a reply. Returns the new ones."""
    found = [m.strip() for m in REMEMBER_RE.findall(CODE_BLOCK.sub("", reply))]
    if not found:
        return []
    notes = load_notes()
    have = {ln.lstrip("- ").strip().lower() for ln in notes.splitlines()}
    new = [f for f in found if f and f.lower() not in have]
    if new:
        body = notes.rstrip("\n")
        body += ("\n" if body else "") + "\n".join(f"- {f}" for f in new)
        save_notes(body)
    return new


# ----------------------------------------------------------------- digest

def outcome_of(messages: list) -> str:
    """How a chat ended: its DONE summary, else the last reply, without code."""
    replies = [m["content"] for m in messages if m.get("role") == "assistant"]
    if not replies:
        return ""
    done = [r for r in replies if DONE_RE.search(r)]
    text = CODE_BLOCK.sub("[command]", (done or replies)[-1])
    text = " ".join(text.split())
    return text[:OUTCOME_CHARS] + ("…" if len(text) > OUTCOME_CHARS else "")


def _chat_digest(data: dict) -> str:
    when = time.strftime("%Y-%m-%d %H:%M",
                         time.localtime(data.get("updated", 0)))
    lines = [f"### {when} — {data.get('title', 'Untitled chat')}"]
    log = data.get("log", [])
    if log:
        shown = log[-COMMANDS_PER_CHAT:]
        lines.append("Commands run" + (f" (last {len(shown)} of {len(log)})"
                                       if len(log) > len(shown) else "") + ":")
        for entry in shown:
            cmd = " ".join(entry.get("command", "").split())
            if len(cmd) > 160:
                cmd = cmd[:160] + "…"
            lines.append(f"- `{cmd}` — {entry.get('result', 'ran')}")
    outcome = outcome_of(data.get("messages", []))
    if outcome:
        lines.append(f"How it ended: {outcome}")
    return "\n".join(lines)


def memory_prompt(sessions: int, exclude: str = "", budget: int = MEMORY_CHARS) -> str:
    """Text appended to the system prompt. Empty when memory is off."""
    if sessions <= 0:
        return ""
    parts = [
        "## Memory from earlier sessions",
        "This app keeps a memory between chats. Use it for context — hosts, "
        "paths, what was already fixed or tried — but the machine may have "
        "changed since, so check the current state before relying on it, and "
        "do not re-run commands just because they are listed here.",
        "To save a lasting fact for future chats (a host or path, a fix that "
        "worked, a preference I state), put it on its own line starting with "
        "`REMEMBER:`, outside any code block. Save only what will matter later.",
    ]
    notes = load_notes().strip()
    if notes:
        parts.append("### Saved notes\n" + notes)

    used = sum(len(p) for p in parts)
    chats = [c for c in list_chats(limit=sessions + 1) if c["id"] != exclude]
    digests = []
    for c in chats[:sessions]:
        d = _chat_digest(c["data"])
        if used + len(d) > budget:
            break
        digests.append(d)
        used += len(d)
    if digests:
        parts.append("### Recent chats, newest first\n\n" + "\n\n".join(digests))
    elif not notes:
        return ""
    text = "\n\n".join(parts)
    return text[:budget]
