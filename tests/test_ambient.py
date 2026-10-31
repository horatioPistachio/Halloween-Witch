"""AmbientPlayer and PhraseCache with a fake voice and speaker."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from witch.ambient import AmbientPlayer, PhraseCache, build_ambient
from witch.audio.base import PlaybackError
from witch.bag import ShuffleBag
from witch.config import load_settings
from witch.tts.base import TTSError

SETTINGS = load_settings(
    trigger="keyboard",
    environ={"OPENAI_API_KEY": "sk-test", "ELEVEN_LABS_API_KEY": "el-test", "WITCH_TTS_MODEL": "eleven_v4_turbo"},
)
AUDIO = [b"\x01\x00" * 100, b"\x02\x00" * 100]


class FakeTTS:
    def __init__(self, fail: bool = False) -> None:
        self.requests: list[str] = []
        self._fail = fail

    async def __aenter__(self) -> FakeTTS:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        self.requests.append(text)
        if self._fail:
            raise TTSError("boom")
        for chunk in AUDIO:
            yield chunk


class FakeSink:
    def __init__(self, fail_after_first: bool = False, hang: bool = False) -> None:
        self.played: list[bytes] = []
        self._fail = fail_after_first
        self._hang = hang

    async def __aenter__(self) -> FakeSink:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def play(self, chunks: AsyncIterator[bytes]) -> None:
        received = bytearray()
        async for chunk in chunks:
            received += chunk
            if self._fail:
                raise PlaybackError("speaker gone")
            if self._hang:
                await asyncio.Event().wait()
        self.played.append(bytes(received))


def _player(tmp_path, tts=None, sink=None, settings=SETTINGS, phrases=("[bored] Is it midnight yet?",)):
    tts = tts or FakeTTS()
    sink = sink or FakeSink()
    cache = PhraseCache(tmp_path)
    player = AmbientPlayer(settings, ShuffleBag(phrases), cache, lambda _s: tts, lambda _s: sink)
    return player, tts, sink


def test_a_cache_miss_synthesises_plays_and_caches_then_a_hit_makes_no_request(tmp_path):
    async def scenario() -> None:
        player, tts, sink = _player(tmp_path)
        await player.play_next()
        assert len(tts.requests) == 1
        assert sink.played == [b"".join(AUDIO)]
        assert len(list(tmp_path.glob("*.pcm"))) == 1

        await player.play_next()
        assert len(tts.requests) == 1
        assert sink.played[1] == b"".join(AUDIO)

    asyncio.run(scenario())


def test_ambient_audio_goes_through_the_eyes_on_both_paths(tmp_path):
    class RecordingEyes:
        def __init__(self) -> None:
            self.chunks = 0
            self.sessions = 0

        @asynccontextmanager
        async def track(self, chunks: AsyncIterator[bytes]) -> AsyncIterator[AsyncIterator[bytes]]:
            self.sessions += 1

            async def counted() -> AsyncIterator[bytes]:
                async for chunk in chunks:
                    self.chunks += 1
                    yield chunk

            yield counted()

    async def scenario() -> None:
        eyes = RecordingEyes()
        tts, sink = FakeTTS(), FakeSink()
        player = AmbientPlayer(
            SETTINGS,
            ShuffleBag(("[bored] Is it midnight yet?",)),
            PhraseCache(tmp_path),
            lambda _s: tts,
            lambda _s: sink,
            eyes=eyes,  # type: ignore[arg-type]
        )
        await player.play_next()  # synthesised
        await player.play_next()  # cached
        assert eyes.sessions == 2
        assert eyes.chunks == len(AUDIO) + 1  # the cache replays the clip as one chunk
        assert len(sink.played) == 2

    asyncio.run(scenario())


def test_the_spoken_text_keeps_tags_only_for_tag_capable_models(tmp_path):
    async def scenario() -> None:
        player, tts, _ = _player(tmp_path)
        await player.play_next()
        assert tts.requests[0].startswith("[bored]")

        plain = replace(SETTINGS, tts_model="eleven_turbo_v2_5")
        player, tts, _ = _player(tmp_path / "other", settings=plain)
        await player.play_next()
        assert "[" not in tts.requests[0]

    asyncio.run(scenario())


def test_voice_model_and_stability_change_the_cache_key():
    keys = {
        PhraseCache.key(SETTINGS, "hello"),
        PhraseCache.key(replace(SETTINGS, voice_id="other"), "hello"),
        PhraseCache.key(replace(SETTINGS, tts_model="eleven_v3"), "hello"),
        PhraseCache.key(replace(SETTINGS, tts_stability=0.3), "hello"),
        PhraseCache.key(SETTINGS, "goodbye"),
    }
    assert len(keys) == 5


def test_a_failed_or_interrupted_playback_caches_nothing(tmp_path):
    async def scenario() -> None:
        player, _tts, _sink = _player(tmp_path, sink=FakeSink(fail_after_first=True))
        await player.play_next()
        assert list(tmp_path.iterdir()) == []

    asyncio.run(scenario())


def test_tts_errors_are_swallowed_and_logged(tmp_path, caplog):
    async def scenario() -> None:
        player, _tts, _sink = _player(tmp_path, tts=FakeTTS(fail=True))
        with caplog.at_level(logging.WARNING, logger="witch.ambient"):
            await player.play_next()
        assert "TTS failed" in caplog.text
        assert list(tmp_path.iterdir()) == []

    asyncio.run(scenario())


def test_a_hung_speaker_is_abandoned_after_the_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr("witch.ambient.AMBIENT_TIMEOUT_S", 0.05)

    async def scenario() -> None:
        player, _tts, _sink = _player(tmp_path, sink=FakeSink(hang=True))
        await asyncio.wait_for(player.play_next(), 2)

    asyncio.run(scenario())


def test_an_odd_sized_cache_file_is_discarded_and_resynthesised(tmp_path):
    async def scenario() -> None:
        player, tts, _ = _player(tmp_path)
        await player.play_next()
        (cached,) = tmp_path.glob("*.pcm")
        cached.write_bytes(b"\x01\x02\x03")
        await player.play_next()
        assert len(tts.requests) == 2
        assert cached.read_bytes() == b"".join(AUDIO)

    asyncio.run(scenario())


def test_without_a_cache_directory_every_play_synthesises():
    async def scenario() -> None:
        tts, sink = FakeTTS(), FakeSink()
        player = AmbientPlayer(
            SETTINGS, ShuffleBag(["hi there"]), PhraseCache(None), lambda _s: tts, lambda _s: sink
        )
        await player.play_next()
        await player.play_next()
        assert len(tts.requests) == 2

    asyncio.run(scenario())


def test_an_unwritable_cache_is_not_fatal(tmp_path, caplog):
    async def scenario() -> None:
        blocker = tmp_path / "file"
        blocker.write_text("x")
        player = AmbientPlayer(
            SETTINGS,
            ShuffleBag(["hi there"]),
            PhraseCache(blocker / "sub"),
            lambda _s: FakeTTS(),
            lambda _s: FakeSink(),
        )
        with caplog.at_level(logging.WARNING, logger="witch.ambient"):
            await player.play_next()
        assert "Phrase cache problem" in caplog.text

    asyncio.run(scenario())


def test_phrases_are_drawn_from_the_bag_without_immediate_repeats(tmp_path):
    async def scenario() -> None:
        phrases = ("one one", "two two", "three three")
        player, tts, _ = _player(tmp_path, phrases=phrases)
        for _ in range(6):
            await player.play_next()
        # Cached after the first round, so only three requests, one per phrase.
        assert sorted(tts.requests) == sorted(phrases)

    asyncio.run(scenario())


def test_build_ambient_is_off_when_disabled():
    assert build_ambient(SETTINGS) is not None
    assert build_ambient(replace(SETTINGS, ambient_interval_s=0)) is None
    assert build_ambient(replace(SETTINGS, tts_enabled=False)) is None
    assert build_ambient(SETTINGS, phrases=()) is None


@pytest.mark.parametrize("n", [1])
def test_cancellation_propagates(tmp_path, n):
    async def scenario() -> None:
        player, _tts, _sink = _player(tmp_path, sink=FakeSink(hang=True))
        task = asyncio.create_task(player.play_next())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
