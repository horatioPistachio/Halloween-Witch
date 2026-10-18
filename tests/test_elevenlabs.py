"""ElevenLabs client tests using httpx.MockTransport (no network)."""

import asyncio
import json

import httpx
import pytest

from witch.config import load_settings
from witch.tts.base import TTSError
from witch.tts.elevenlabs import ElevenLabsTTS

VOICE = "xBIfEFAROTtux2FvqTLj"
KEY = "el-test-456"


class ByteParts(httpx.AsyncByteStream):
    def __init__(self, parts: list[bytes]) -> None:
        self._parts = parts

    async def __aiter__(self):
        for part in self._parts:
            yield part


def settings(**extra: str):
    return load_settings(
        tts=True,
        environ={"OPENAI_API_KEY": "sk-test-123", "ELEVEN_LABS_API_KEY": KEY, **extra},
    )


def synthesize(handler, text: str = "Hello", **extra: str) -> bytes:
    async def go() -> bytes:
        transport = httpx.MockTransport(handler)
        async with ElevenLabsTTS(settings(**extra), transport=transport) as tts:
            return b"".join([chunk async for chunk in tts.synthesize(text)])

    return asyncio.run(go())


def test_request_shape():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers["xi-api-key"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=b"\x00\x00")

    synthesize(handler, "[whispers] Hi")

    assert seen["url"] == f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE}/stream?output_format=pcm_24000"
    assert seen["key"] == KEY
    assert seen["body"] == {"text": "[whispers] Hi", "model_id": "eleven_v4_turbo"}


def test_stability_sent_only_when_configured():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=b"\x00\x00")

    synthesize(handler, WITCH_TTS_STABILITY="0.5")
    assert seen["body"]["voice_settings"] == {"stability": 0.5}


def test_odd_byte_boundaries_are_realigned():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=ByteParts([b"\x01\x02\x03", b"\x04\x05\x06\x07"]))

    assert synthesize(handler) == b"\x01\x02\x03\x04\x05\x06"


@pytest.mark.parametrize("status", [401, 402, 422, 500])
def test_http_errors_raise_tts_error_without_key(status):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": {"status": "x", "message": "nope"}})

    with pytest.raises(TTSError, match=f"HTTP {status}: nope") as info:
        synthesize(handler)
    assert KEY not in str(info.value)


def test_validation_error_detail_is_summarised():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": [{"loc": ["body"], "msg": "bad", "type": "x"}]})

    with pytest.raises(TTSError, match="request validation failed"):
        synthesize(handler)


def test_transport_timeout_raises_tts_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(TTSError, match="ReadTimeout"):
        synthesize(handler)
