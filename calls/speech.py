"""Reply text to speakable sentences. Pure."""

import re

HANGUP = "[[hangup]]"
_FENCE = re.compile(r"```.*?```", re.DOTALL)
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_URL = re.compile(r"https?://\S+")
_MARKUP = re.compile(r"[*_#>`]+|^\s*[-+]\s+|^\s*\d+\.\s+", re.MULTILINE)
_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_CLAUSE = re.compile(r"[,;:]\s")
FIRST_MAX = 70  # longer first chunks are split at a clause so speech starts sooner
MIN_SENTENCE = 25  # shorter pieces are merged forward: each TTS call costs ~1 s


# What Whisper-style models "hear" in room noise or silence.
_PHANTOMS = {"thank you", "thanks", "thank you for watching", "thanks for watching", "you", "bye"}
PHANTOM_MAX_S = 3.0


def is_phantom(text: str, seconds: float) -> bool:
    """A short utterance whose transcript is a stock phrase: noise, not the caller."""
    return seconds < PHANTOM_MAX_S and re.sub(r"[^a-z ]", "", text.lower()).strip() in _PHANTOMS


def clean(text: str) -> str:
    """Documented rule: code blocks become "code omitted", links keep their label, bare URLs
    become "a link", markdown marks and list bullets are dropped."""
    text = _FENCE.sub(" code omitted. ", text)
    text = _LINK.sub(r"\1", text)
    text = _URL.sub("a link", text)
    return _MARKUP.sub("", text).strip()


def split_hangup(text: str) -> tuple[str, bool]:
    return text.replace(HANGUP, "").strip(), HANGUP in text


def sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SPLIT.split(clean(text)) if p.strip()]
    merged: list[str] = []
    carry = ""
    for part in parts:
        carry = f"{carry} {part}".strip()
        if len(carry) >= MIN_SENTENCE:
            merged.append(carry)
            carry = ""
    if carry and merged:
        merged[-1] = f"{merged[-1]} {carry}"
    elif carry:
        merged.append(carry)
    return _quick_start(merged)


def _quick_start(parts: list[str]) -> list[str]:
    """Split a long first chunk at its first comma: TTS time grows with text length."""
    if parts and len(parts[0]) > FIRST_MAX:
        cut = _CLAUSE.search(parts[0], MIN_SENTENCE)
        if cut:
            return [parts[0][: cut.end()].strip(), parts[0][cut.end() :].strip(), *parts[1:]]
    return parts
