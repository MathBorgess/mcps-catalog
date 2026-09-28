"""Clip contract, text/label validation and sentence chunking for local playback."""

import re

from pydantic import BaseModel

MAX_TEXT = 12000
MAX_CLIP_TEXT = 3000
MAX_CLIPS = 20
MAX_NOTE = 3000

LINK_RE = re.compile(r"://|www\.", re.IGNORECASE)
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
CONTROL_RE_ALLOW_NEWLINES = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")

SENTENCE_END_RE = re.compile(r"(?<=[.!?…])\s+")
PARAGRAPH_RE = re.compile(r"\n\s*\n+")


class Clip(BaseModel):
    text: str
    caption: str | None = None


def text_error(text: str, max_len: int) -> str | None:
    if not 1 <= len(text) <= max_len:
        return f"text must be 1-{max_len} chars"
    return None


def label_error(label: str | None, field: str, max_len: int = 300,
                 allow_newlines: bool = False) -> str | None:
    if label is None:
        return None
    if not 1 <= len(label) <= max_len:
        return f"{field} must be 1-{max_len} chars"
    control_re = CONTROL_RE_ALLOW_NEWLINES if allow_newlines else CONTROL_RE
    if control_re.search(label):
        suffix = "" if allow_newlines else " or newlines"
        return f"{field} must not contain control characters{suffix}"
    if LINK_RE.search(label):
        return f"{field} must not contain a link"
    return None


def _hard_split(sentence: str, max_chars: int) -> list[str]:
    pieces: list[str] = []
    s = sentence
    while len(s) > max_chars:
        cut = s.rfind(" ", 0, max_chars + 1)
        if cut <= 0:
            cut = max_chars
        pieces.append(s[:cut].strip())
        s = s[cut:].strip()
    pieces.append(s)
    return pieces


def chunk_text(text: str, max_chars: int = 400) -> list[str]:
    chunks: list[str] = []
    for para in PARAGRAPH_RE.split(text.strip()):
        para = para.strip()
        if not para:
            continue
        sentences = [s.strip() for s in SENTENCE_END_RE.split(para) if s.strip()]
        current = ""
        for sentence in sentences:
            candidate = f"{current} {sentence}" if current else sentence
            if len(candidate) <= max_chars:
                current = candidate
                continue
            if current:
                chunks.append(current)
                current = ""
            if len(sentence) <= max_chars:
                current = sentence
            else:
                pieces = _hard_split(sentence, max_chars)
                chunks.extend(pieces[:-1])
                current = pieces[-1]
        if current:
            chunks.append(current)
    return chunks
