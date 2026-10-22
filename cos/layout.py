"""Names and grouping policy for Nio-owned Herdr lanes."""

from __future__ import annotations

import os
import re

from .controller import structured


TITLE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": "A concrete, action-oriented title of 3-7 words.",
        },
    },
}
TITLE_PROMPT = """Write a succinct task title for the owner's request below.
Use 3-7 words, start with an action verb, and name the actual subject.
Do not copy conversational wording, add punctuation, or include an issue/PR number.
Return only the structured title field.

OWNER REQUEST:
{prompt}"""


def group_for_task(prompt: str, cwd: str) -> str:
    text = f"{prompt}\n{cwd}".lower()
    if re.search(r"\b(release|deploy|ship|promot(?:e|ion)|rollout|version bump|tag v?\d)", text):
        return "Nio · Releases"
    if re.search(r"\b(sentry|alert|incident|outage|production error|logs?|monitor|watch|ci failure)", text):
        return "Nio · Operations"
    if re.search(r"\b(nio|neo|herdr|slack bot|agent routing|automation)\b", text) or "/neo" in text:
        return "Nio · Agent operations"
    if re.search(r"(?:\b(?:issue|pr|pull request)\b|#\d+)", text):
        return "Nio · Issue & PR fixes"
    base = os.path.basename(os.path.normpath(cwd)).lower()
    if "backie" in base:
        return "Nio · Backend fixes"
    if "deskie" in base:
        return "Nio · Desktop fixes"
    if "mobile" in base:
        return "Nio · Mobile fixes"
    if "design" in base:
        return "Nio · Product design"
    return "Nio · Product work"


def _fallback_title(prompt: str, limit: int = 48) -> str:
    text = re.sub(r"\s+", " ", prompt).strip()
    text = re.sub(
        r"^(?:(?:can|could|would)\s+you\s+|i\s+(?:want|would like)\s+(?:you\s+)?to\s+|please\s+)+",
        "",
        text,
        flags=re.I,
    )
    text = re.split(r"\s+(?:and|but|so)\s+", text, maxsplit=1, flags=re.I)[0]
    delivery = re.fullmatch(r"check why (.+?) messages? (?:is|are) being sent", text, re.I)
    if delivery:
        text = f"Investigate {delivery.group(1)} message delivery"
    words = text.strip(" .?!,:;-").split()
    text = " ".join(words[:7]) or "New task"
    text = text[:1].upper() + text[1:]
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] or text[:limit]
    return text


def _task_parts(prompt: str) -> tuple[str | None, str]:
    text = re.sub(r"\s+", " ", prompt).strip()
    reference = re.search(
        r"(?:\b(?:issue|pr|pull request)\s*#?\s*(\d+)\b|github\.com/[^\s]+/pull/(\d+)\b|#(\d+)\b)",
        text,
        re.I,
    )
    number = next((value for value in reference.groups() if value), None) if reference else None
    if reference:
        text = (text[:reference.start()] + " " + text[reference.end():]).strip(" :-")
        text = re.sub(r"\s*:\s*", " ", text, count=1)
    return number, text


def task_label(prompt: str, limit: int = 56) -> str:
    """Return immediately with a bounded deterministic title for lane creation."""
    number, text = _task_parts(prompt)
    title = _fallback_title(text)
    if number:
        title = f"#{number} {title}"
    return title if len(title) <= limit else title[:limit].rsplit(" ", 1)[0]


def suggest_task_label(prompt: str, limit: int = 56) -> str:
    """Best-effort semantic title used to rename an already-running lane."""
    number, text = _task_parts(prompt)
    result = structured(TITLE_PROMPT.format(prompt=text[:2000]), TITLE_SCHEMA,
                        "claude-haiku-4-5", 30, "nio-task-title")
    generated = re.sub(r"\s+", " ", str((result or {}).get("title") or "")).strip(" .?!,:;-")
    words = generated.split()
    title = generated if 3 <= len(words) <= 7 and len(generated) <= limit else _fallback_title(text)
    if number and not re.search(rf"#\s*{re.escape(number)}\b", title):
        title = f"#{number} {title}"
    return title if len(title) <= limit else title[:limit].rsplit(" ", 1)[0]
