"""Reply text to speakable sentences. Pure."""

import re

HANGUP = "[[hangup]]"
_FENCE = re.compile(r"```.*?```", re.DOTALL)
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_URL = re.compile(r"https?://\S+")
_MARKUP = re.compile(r"[*_#>`]+|^\s*[-+]\s+|^\s*\d+\.\s+", re.MULTILINE)
_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
MIN_SENTENCE = 25  # shorter pieces are merged forward: each TTS call costs ~1 s


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
    return merged
