"""Parses witch replies into speech text and performance tags."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from witch.persona import ALLOWED_TAGS

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"\[([A-Za-z][A-Za-z \-]{0,31})\]")
_SPACES_RE = re.compile(r"\s+")
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([.,!?])")
MAX_TTS_CHARS = 1_000


@dataclass(frozen=True)
class Segment:
    tag: str | None
    text: str


@dataclass(frozen=True)
class WitchResponse:
    raw: str
    clean_text: str
    tags: tuple[str, ...]
    segments: tuple[Segment, ...]
    unknown_tags: tuple[str, ...]


def parse(raw: str) -> WitchResponse:
    tags: list[str] = []
    unknown: list[str] = []
    # Each piece is (tag that opened it, text fragments that follow it).
    pieces: list[tuple[str | None, list[str]]] = [(None, [])]
    last = 0

    for match in _TAG_RE.finditer(raw):
        pieces[-1][1].append(raw[last : match.start()])
        last = match.end()
        name = " ".join(match.group(1).lower().split())
        if name in ALLOWED_TAGS:
            tags.append(name)
            pieces.append((name, []))
        else:
            unknown.append(name)
            log.warning("Dropped unknown tag [%s] from model reply", match.group(1))
    pieces[-1][1].append(raw[last:])

    segments = []
    for tag, parts in pieces:
        text = _normalise_spaces("".join(parts))
        if tag is None and not text:
            continue
        segments.append(Segment(tag=tag, text=text))

    clean = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", _normalise_spaces(_TAG_RE.sub("", raw)))

    return WitchResponse(
        raw=raw,
        clean_text=clean,
        tags=tuple(tags),
        segments=tuple(segments),
        unknown_tags=tuple(unknown),
    )


def _normalise_spaces(text: str) -> str:
    return _SPACES_RE.sub(" ", text).strip()


def speech_text(response: WitchResponse, *, keep_tags: bool) -> str:
    """Text to send to TTS; an empty string means there is nothing to say."""
    if not keep_tags:
        text = response.clean_text
    else:
        pieces = []
        for segment in response.segments:
            if segment.tag is None:
                if segment.text:
                    pieces.append(segment.text)
            else:
                pieces.append(f"[{segment.tag}] {segment.text}".rstrip())
        text = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", _normalise_spaces(" ".join(pieces)))
    return _truncate(text.strip())


def trusted_speech_text(raw: str, *, keep_tags: bool) -> str:
    """Speech text for operator-written phrases: any [tag] is kept as is (no whitelist) or stripped."""
    text = raw if keep_tags else _TAG_RE.sub("", raw)
    text = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", _normalise_spaces(text))
    return _truncate(text)


def _truncate(text: str) -> str:
    if len(text) <= MAX_TTS_CHARS:
        return text
    log.warning("Truncating TTS text from %d to %d chars", len(text), MAX_TTS_CHARS)
    head = text[:MAX_TTS_CHARS]
    open_bracket = head.rfind("[")
    if open_bracket > head.rfind("]"):
        head = head[:open_bracket]
    sentence_end = max(head.rfind(mark) for mark in ".!?")
    return head[: sentence_end + 1] if sentence_end > 0 else head.rstrip()
