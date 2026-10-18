"""Cost calculation and per-interaction tracking tests."""

import json
from types import SimpleNamespace as NS

import pytest

from witch.cost import PRICES, CostTracker, TokenUsage, cost_usd, format_interaction, tokens_from_usage

MODEL = "gpt-realtime-2.1-mini"


def test_cost_matches_published_prices():
    tokens = TokenUsage(text_in=1000, text_in_cached=400, audio_in=2000, audio_in_cached=1000, text_out=500)
    expected = (600 * 0.60 + 400 * 0.06 + 1000 * 10.00 + 1000 * 0.30 + 500 * 2.40) / 1_000_000
    assert cost_usd(tokens, PRICES[MODEL]) == pytest.approx(expected)


def test_tokens_from_sdk_shaped_usage():
    usage = NS(
        input_tokens=400,
        output_tokens=50,
        input_token_details=NS(
            text_tokens=100,
            audio_tokens=300,
            image_tokens=0,
            cached_tokens=120,
            cached_tokens_details=NS(text_tokens=20, audio_tokens=100, image_tokens=0),
        ),
        output_token_details=NS(text_tokens=50, audio_tokens=0),
    )
    t = tokens_from_usage(usage)
    assert (t.text_in, t.text_in_cached) == (100, 20)
    assert (t.audio_in, t.audio_in_cached) == (300, 100)
    assert t.text_out == 50
    assert t.audio_out == 0


def test_reasoning_tokens_counted_as_text_output():
    usage = NS(output_tokens=200, input_token_details=None, output_token_details=NS(text_tokens=None, audio_tokens=0))
    assert tokens_from_usage(usage).text_out == 200


def test_missing_usage_is_zero():
    assert tokens_from_usage(None) == TokenUsage()


def test_tracker_totals_and_writes_jsonl(tmp_path):
    log_path = tmp_path / "usage.jsonl"
    tracker = CostTracker(MODEL, log_path)

    first = tracker.record("resp_1", "completed", TokenUsage(text_in=1_000_000))
    second = tracker.record("resp_2", "completed", TokenUsage(text_out=1_000_000))

    assert first.usd == pytest.approx(0.60)
    assert second.usd == pytest.approx(2.40)
    assert second.total_usd == pytest.approx(3.00)
    assert tracker.interactions == 2

    lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert [line["number"] for line in lines] == [1, 2]
    assert lines[1]["response_id"] == "resp_2"
    assert "$" not in log_path.read_text(encoding="utf-8")


def test_unknown_model_reports_tokens_without_cost(tmp_path):
    tracker = CostTracker("some-other-model", None)
    entry = tracker.record("resp_1", "completed", TokenUsage(text_in=10))
    assert entry.usd is None
    assert tracker.total_usd is None
    assert "n/a" in format_interaction(entry)
