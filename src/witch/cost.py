"""Token usage and USD cost per interaction for Realtime responses."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

TOKENS_PER_UNIT = 1_000_000


@dataclass(frozen=True)
class ModelPrices:
    """USD per 1M tokens."""

    text_in: float
    text_in_cached: float
    audio_in: float
    audio_in_cached: float
    image_in: float
    image_in_cached: float
    text_out: float
    audio_out: float


# Source: developers.openai.com/api/docs/pricing (checked 2026-10-18).
PRICES: dict[str, ModelPrices] = {
    "gpt-realtime-2.1": ModelPrices(
        text_in=4.00,
        text_in_cached=0.40,
        audio_in=32.00,
        audio_in_cached=0.40,
        image_in=5.00,
        image_in_cached=0.50,
        text_out=24.00,
        audio_out=64.00,
    ),
    "gpt-realtime-2.1-mini": ModelPrices(
        text_in=0.60,
        text_in_cached=0.06,
        audio_in=10.00,
        audio_in_cached=0.30,
        image_in=0.80,
        image_in_cached=0.08,
        text_out=2.40,
        audio_out=20.00,
    ),
}


@dataclass(frozen=True)
class TokenUsage:
    """Input totals include the cached counts, as reported by the API."""

    text_in: int = 0
    text_in_cached: int = 0
    audio_in: int = 0
    audio_in_cached: int = 0
    image_in: int = 0
    image_in_cached: int = 0
    text_out: int = 0
    audio_out: int = 0


@dataclass(frozen=True)
class InteractionCost:
    number: int
    response_id: str
    status: str
    tokens: TokenUsage
    usd: float | None
    total_usd: float | None
    tts_chars: int = 0


def tokens_from_usage(usage: Any) -> TokenUsage:
    """Convert a Realtime `response.usage` object (or None) into TokenUsage."""
    inp = getattr(usage, "input_token_details", None)
    out = getattr(usage, "output_token_details", None)
    cached = getattr(inp, "cached_tokens_details", None)

    audio_out = _count(out, "audio_tokens")
    total_out = getattr(usage, "output_tokens", None)
    # Reasoning tokens are not itemised, so text output is the remainder after audio.
    text_out = _count(out, "text_tokens") if total_out is None else max(int(total_out) - audio_out, 0)

    return TokenUsage(
        text_in=_count(inp, "text_tokens"),
        text_in_cached=_count(cached, "text_tokens"),
        audio_in=_count(inp, "audio_tokens"),
        audio_in_cached=_count(cached, "audio_tokens"),
        image_in=_count(inp, "image_tokens"),
        image_in_cached=_count(cached, "image_tokens"),
        text_out=text_out,
        audio_out=audio_out,
    )


def cost_usd(tokens: TokenUsage, prices: ModelPrices) -> float:
    uncached_text = max(tokens.text_in - tokens.text_in_cached, 0)
    uncached_audio = max(tokens.audio_in - tokens.audio_in_cached, 0)
    uncached_image = max(tokens.image_in - tokens.image_in_cached, 0)
    usd = (
        uncached_text * prices.text_in
        + tokens.text_in_cached * prices.text_in_cached
        + uncached_audio * prices.audio_in
        + tokens.audio_in_cached * prices.audio_in_cached
        + uncached_image * prices.image_in
        + tokens.image_in_cached * prices.image_in_cached
        + tokens.text_out * prices.text_out
        + tokens.audio_out * prices.audio_out
    )
    return usd / TOKENS_PER_UNIT


class CostTracker:
    """Costs each response and keeps a running total; appends one JSON line per interaction."""

    def __init__(self, model: str, log_path: Path | None) -> None:
        self._model = model
        self._prices = PRICES.get(model)
        self._log_path = log_path
        self._interactions = 0
        self._total_usd = 0.0
        self._log_failed = False
        if self._prices is None:
            log.warning("No price table for model %s; token counts only, no cost", model)

    @property
    def interactions(self) -> int:
        return self._interactions

    @property
    def total_usd(self) -> float | None:
        return self._total_usd if self._prices is not None else None

    def record(
        self, response_id: str, status: str, tokens: TokenUsage, tts_chars: int = 0
    ) -> InteractionCost:
        self._interactions += 1
        usd = cost_usd(tokens, self._prices) if self._prices is not None else None
        if usd is not None:
            self._total_usd += usd
        entry = InteractionCost(
            number=self._interactions,
            response_id=response_id,
            status=status,
            tokens=tokens,
            usd=usd,
            total_usd=self.total_usd,
            tts_chars=tts_chars,
        )
        self._append(entry)
        return entry

    def _append(self, entry: InteractionCost) -> None:
        if self._log_path is None:
            return
        line = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "model": self._model,
            "number": entry.number,
            "response_id": entry.response_id,
            "status": entry.status,
            "tokens": asdict(entry.tokens),
            "usd": entry.usd,
            "total_usd": entry.total_usd,
            "tts_chars": entry.tts_chars,
        }
        try:
            with self._log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(line) + "\n")
        except OSError as exc:
            if not self._log_failed:
                log.warning("Cannot write usage log %s: %s", self._log_path, exc)
                self._log_failed = True


def format_interaction(entry: InteractionCost) -> str:
    t = entry.tokens
    cached = t.text_in_cached + t.audio_in_cached + t.image_in_cached
    usd = "n/a" if entry.usd is None else f"${entry.usd:.5f}"
    total = "n/a" if entry.total_usd is None else f"${entry.total_usd:.4f}"
    line = (
        f"cost {usd} | in: audio {t.audio_in}, text {t.text_in}, image {t.image_in}, "
        f"cached {cached} | out: text {t.text_out}, audio {t.audio_out} | "
        f"run total {total} after {entry.number} interactions"
    )
    if entry.tts_chars:
        line += f" | tts chars {entry.tts_chars}"
    return line


def _count(obj: Any, name: str) -> int:
    if obj is None:
        return 0
    return int(getattr(obj, name, None) or 0)
