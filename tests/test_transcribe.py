"""Tests for app.transcribe. No network: httpx.MockTransport and scripted fake providers."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace

import httpx
import pytest

from app import transcribe as tr
from app.models import AppError, AudioInfo, Chunk, Segment

KEY = "gsk_test_key_123"
AUDIO_BYTES = b"ID3-fake-audio-bytes"
OK_BODY = {"language": "english", "segments": [{"start": 0.0, "end": 2.5, "text": " Hello world."}]}
INFO = AudioInfo(path="/tmp/original.mp3", duration_s=1500.0, title="My Talk", source="https://example.com/v")


def run(coro):
    return asyncio.run(coro)


def make_settings(**over):
    base = dict(
        provider="groq", groq_api_key=KEY, groq_model="whisper-large-v3-turbo",
        cf_account_id="acc123", cf_api_token="cf_token_456",
        provider_concurrency=3, cost_per_hour_usd=0.04, chunk_seconds=600,
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def fake_settings(monkeypatch):
    monkeypatch.setattr(tr, "settings", make_settings())


@pytest.fixture
def sleeps(monkeypatch):
    """Replace the backoff sleep; the list records every wait that was requested."""
    waited: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waited.append(seconds)

    monkeypatch.setattr(tr, "_sleep", fake_sleep)
    return waited


@pytest.fixture
def audio(tmp_path):
    path = tmp_path / "chunk.mp3"
    path.write_bytes(AUDIO_BYTES)
    return str(path)


def groq(handler):
    return tr.GroqProvider(transport=httpx.MockTransport(handler))


def cloudflare(handler):
    return tr.CloudflareProvider(transport=httpx.MockTransport(handler))


# ------------------------------------------------------------------ Groq provider

def test_groq_success_and_request_shape(audio):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(url=str(request.url), auth=request.headers["authorization"],
                    ctype=request.headers["content-type"], body=request.content)
        return httpx.Response(200, json={"language": "english", "segments": [
            {"start": 0.0, "end": 2.5, "text": " Hello world."},
            {"start": 2.5, "end": 4.0, "text": "  "},
        ]})

    lang, segs = run(groq(handler).transcribe(audio, None))
    assert lang == "en"
    assert segs[0] == Segment(0.0, 2.5, "Hello world.")
    assert segs[1].text == ""  # blanks are dropped later, by transcribe_chunks
    assert seen["url"] == tr.GROQ_URL
    assert seen["auth"] == f"Bearer {KEY}"
    assert seen["ctype"].startswith("multipart/form-data")
    for needle in (
        b'name="model"', b"whisper-large-v3-turbo", b'name="response_format"', b"verbose_json",
        b'name="timestamp_granularities[]"', b"segment", b'name="temperature"',
        b'name="file"', AUDIO_BYTES,
    ):
        assert needle in seen["body"]
    assert b'name="language"' not in seen["body"]


def test_groq_sends_language_only_when_given(audio):
    seen = {}

    def handler(request):
        seen["body"] = request.content
        return httpx.Response(200, json={"language": "es", "segments": []})

    assert run(groq(handler).transcribe(audio, "es")) == ("es", [])
    assert b'name="language"\r\n\r\nes\r\n' in seen["body"]


def test_groq_text_without_segments_becomes_one_segment(audio):
    def handler(request):
        return httpx.Response(200, json={"language": "en", "text": " Just text ", "duration": 4.0})

    assert run(groq(handler).transcribe(audio, None)) == ("en", [Segment(0.0, 4.0, "Just text")])


def test_groq_429_retries_and_respects_retry_after(audio, sleeps):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={"error": "rate limited"})
        return httpx.Response(200, json=OK_BODY)

    lang, segs = run(groq(handler).transcribe(audio, None))
    assert (lang, len(segs)) == ("en", 1)
    assert calls["n"] == 2 and sleeps == [2.0]


def test_groq_5xx_is_retried_then_succeeds(audio, sleeps):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(500) if calls["n"] == 1 else httpx.Response(200, json=OK_BODY)

    run(groq(handler).transcribe(audio, None))
    assert calls["n"] == 2 and sleeps == [1.0]


def test_groq_backoff_schedule_then_provider_error(audio, sleeps):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503, text="upstream body that must not leak")

    with pytest.raises(AppError) as exc:
        run(groq(handler).transcribe(audio, None))
    assert (exc.value.code, exc.value.http_status) == ("PROVIDER_ERROR", 502)
    assert calls["n"] == 4 and sleeps == [1.0, 3.0, 8.0]
    assert "upstream body" not in exc.value.message


def test_groq_401_is_not_retried_and_nothing_leaks(audio, sleeps, caplog):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(401, json={"error": {"message": "Invalid key SECRET-BODY-TEXT"}})

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(AppError) as exc:
            run(groq(handler).transcribe(audio, None))
    assert exc.value.code == "PROVIDER_ERROR" and exc.value.http_status == 502
    assert calls["n"] == 1 and sleeps == []
    for secret in ("SECRET-BODY-TEXT", KEY):
        assert secret not in exc.value.message
        assert secret not in caplog.text


def test_groq_403_is_not_retried(audio, sleeps):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(403)

    with pytest.raises(AppError) as exc:
        run(groq(handler).transcribe(audio, None))
    assert exc.value.code == "PROVIDER_ERROR" and calls["n"] == 1


def test_groq_huge_retry_after_fails_fast(audio, sleeps):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(429, headers={"retry-after": "3600"})

    with pytest.raises(AppError) as exc:
        run(groq(handler).transcribe(audio, None))
    assert exc.value.code == "PROVIDER_ERROR" and calls["n"] == 1 and sleeps == []


def test_groq_network_error_is_retried(audio, sleeps):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json=OK_BODY)

    lang, _ = run(groq(handler).transcribe(audio, None))
    assert lang == "en" and calls["n"] == 2 and sleeps == [1.0]


def test_groq_persistent_network_error(audio, sleeps):
    def handler(request):
        raise httpx.ReadTimeout("too slow", request=request)

    with pytest.raises(AppError) as exc:
        run(groq(handler).transcribe(audio, None))
    assert (exc.value.code, exc.value.http_status) == ("PROVIDER_ERROR", 502)
    assert sleeps == [1.0, 3.0, 8.0]


def test_groq_invalid_json_is_provider_error_without_retry(audio, sleeps):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, text="<html>not json</html>")

    with pytest.raises(AppError) as exc:
        run(groq(handler).transcribe(audio, None))
    assert exc.value.code == "PROVIDER_ERROR" and calls["n"] == 1 and sleeps == []


def test_groq_requires_api_key(monkeypatch):
    monkeypatch.setattr(tr, "settings", make_settings(groq_api_key="  "))
    with pytest.raises(AppError) as exc:
        tr.GroqProvider()
    assert exc.value.code == "INTERNAL"


def test_unreadable_chunk_is_internal_error(tmp_path):
    def handler(request):
        return httpx.Response(200, json=OK_BODY)

    with pytest.raises(AppError) as exc:
        run(groq(handler).transcribe(str(tmp_path / "missing.mp3"), None))
    assert exc.value.code == "INTERNAL"


# ------------------------------------------------------------------ Cloudflare provider

CF_OK = {
    "success": True, "errors": [], "messages": [],
    "result": {
        "text": "Hola mundo", "word_count": 2,
        "transcription_info": {"language": "es", "language_probability": 0.99, "duration": 3.0},
        "segments": [{"start": 0.0, "end": 3.0, "text": " Hola mundo", "words": []}],
    },
}


def test_cloudflare_success_and_request_shape(audio):
    seen = {}

    def handler(request):
        seen.update(url=str(request.url), auth=request.headers["authorization"], body=json.loads(request.content))
        return httpx.Response(200, json=CF_OK)

    lang, segs = run(cloudflare(handler).transcribe(audio, None))
    assert (lang, segs) == ("es", [Segment(0.0, 3.0, "Hola mundo")])
    assert seen["url"] == (
        "https://api.cloudflare.com/client/v4/accounts/acc123/ai/run/@cf/openai/whisper-large-v3-turbo"
    )
    assert seen["auth"] == "Bearer cf_token_456"
    assert base64.b64decode(seen["body"]["audio"]) == AUDIO_BYTES
    assert "language" not in seen["body"]


def test_cloudflare_sends_language_and_accepts_unwrapped_result(audio):
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=CF_OK["result"])

    lang, segs = run(cloudflare(handler).transcribe(audio, "es"))
    assert seen["body"]["language"] == "es" and lang == "es" and len(segs) == 1


def test_cloudflare_success_false_and_retry(audio, sleeps):
    def failing(request):
        return httpx.Response(200, json={"success": False, "result": None,
                                         "errors": [{"code": 10000, "message": "SECRET-DETAIL"}]})

    with pytest.raises(AppError) as exc:
        run(cloudflare(failing).transcribe(audio, None))
    assert exc.value.code == "PROVIDER_ERROR" and "SECRET-DETAIL" not in exc.value.message

    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        return httpx.Response(429) if calls["n"] == 1 else httpx.Response(200, json=CF_OK)

    run(cloudflare(flaky).transcribe(audio, None))
    assert calls["n"] == 2 and sleeps == [1.0]


def test_cloudflare_requires_credentials(monkeypatch):
    monkeypatch.setattr(tr, "settings", make_settings(cf_api_token=""))
    with pytest.raises(AppError) as exc:
        tr.CloudflareProvider()
    assert exc.value.code == "INTERNAL"


def test_cloudflare_error_fields_are_not_logged(audio, caplog):
    secret = "PRIVATE-TRANSCRIPT-OR-TOKEN"

    def handler(request):
        return httpx.Response(200, json={
            "success": False, "errors": [{"code": secret, "message": secret}],
        })

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(AppError) as exc:
            run(cloudflare(handler).transcribe(audio, None))
    assert exc.value.code == "PROVIDER_ERROR"
    assert secret not in caplog.text
    assert secret not in exc.value.message


def test_get_provider_follows_settings(monkeypatch):
    assert isinstance(tr.get_provider(), tr.GroqProvider)
    monkeypatch.setattr(tr, "settings", make_settings(provider="Cloudflare"))
    assert isinstance(tr.get_provider(), tr.CloudflareProvider)
    monkeypatch.setattr(tr, "settings", make_settings(provider="nope"))
    with pytest.raises(AppError) as exc:
        tr.get_provider()
    assert exc.value.code == "INTERNAL"


# ------------------------------------------------------------------ helpers

def test_normalize_language():
    assert tr._normalize_language("English") == "en"
    assert tr._normalize_language("spanish") == "es"
    assert tr._normalize_language("Haitian Creole") == "ht"
    assert tr._normalize_language("en") == "en"
    assert tr._normalize_language("pt-BR") == "pt"
    assert tr._normalize_language("yue") == "yue"
    assert tr._normalize_language("") == ""
    assert tr._normalize_language(None) == ""
    assert tr._normalize_language("not a language") == ""


def test_parse_retry_after():
    assert tr._parse_retry_after("5") == 5.0
    assert tr._parse_retry_after("0.5") == 0.5
    assert tr._parse_retry_after(None) is None
    assert tr._parse_retry_after("soon") is None
    assert tr._parse_retry_after("nan") is None
    assert tr._parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0
    future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True)
    assert 20 < tr._parse_retry_after(future) <= 30


def test_parse_segments_skips_malformed_items():
    raw = [{"start": 1, "end": 2, "text": " ok "}, {"start": None, "end": 2, "text": "no start"},
           {"start": 3, "text": "no end"}, {"start": 5, "end": 4, "text": "reversed"},
           {"start": 6, "end": 7}, "garbage", {"start": "x", "end": 1, "text": "bad"}]
    assert tr._parse_segments(raw) == [Segment(1.0, 2.0, "ok"), Segment(3.0, 3.0, "no end"), Segment(5.0, 5.0, "reversed")]
    assert tr._parse_segments(None) == []


# ------------------------------------------------------------------ transcribe_chunks

class FakeProvider:
    """Scripted provider keyed by chunk path; records the order of calls and concurrency."""

    def __init__(self, results, delays=None, fail=None):
        self.results = results
        self.delays = delays or {}
        self.fail = fail or {}
        self.calls: list[tuple[str, str | None]] = []
        self.events: list[tuple[str, str]] = []
        self.active = 0
        self.max_active = 0
        self.cancelled: list[str] = []

    async def transcribe(self, chunk_path, language):
        self.calls.append((chunk_path, language))
        self.events.append(("start", chunk_path))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delays.get(chunk_path, 0))
            if chunk_path in self.fail:
                raise self.fail[chunk_path]
            self.events.append(("end", chunk_path))
            return self.results[chunk_path]
        except asyncio.CancelledError:
            self.cancelled.append(chunk_path)
            raise
        finally:
            self.active -= 1


def use(monkeypatch, provider):
    monkeypatch.setattr(tr, "get_provider", lambda: provider)
    return provider


def chunks(n):
    return [Chunk(path=f"c{i}.mp3", offset_s=i * 600.0, duration_s=600.0) for i in range(n)]


def empty_results(n):
    return {f"c{i}.mp3": ("en", []) for i in range(n)}


def test_offsets_added_order_and_blank_segments_dropped(monkeypatch):
    fake = use(monkeypatch, FakeProvider(
        {
            "c0.mp3": ("en", [Segment(5.0, 8.0, " first "), Segment(9.0, 9.5, "   ")]),
            "c1.mp3": ("en", [Segment(3.0, 4.0, ""), Segment(0.0, 2.0, "second")]),
            "c2.mp3": ("en", [Segment(1.5, 2.5, "third")]),
        },
        delays={"c0.mp3": 0.05},  # finishes last, results must still be ordered by time
    ))
    t = run(tr.transcribe_chunks(INFO, chunks(3), "en"))
    assert [(s.start, s.end, s.text) for s in t.segments] == [
        (5.0, 8.0, "first"), (600.0, 602.0, "second"), (1201.5, 1202.5, "third"),
    ]
    assert (t.title, t.source, t.duration_s, t.language) == ("My Talk", "https://example.com/v", 1500.0, "en")
    assert len(fake.calls) == 3


def test_language_detected_on_first_chunk_and_reused(monkeypatch):
    fake = use(monkeypatch, FakeProvider(
        {"c0.mp3": ("es", [Segment(0, 1, "hola")]), "c1.mp3": ("en", [Segment(0, 1, "x")]), "c2.mp3": ("en", [])},
        delays={"c0.mp3": 0.03},
    ))
    t = run(tr.transcribe_chunks(INFO, chunks(3), None))
    assert fake.calls[0] == ("c0.mp3", None)
    assert {lang for _, lang in fake.calls[1:]} == {"es"}
    assert fake.events[:2] == [("start", "c0.mp3"), ("end", "c0.mp3")]  # first chunk runs alone
    assert t.language == "es"


def test_unknown_detected_language_falls_back_to_auto_and_und(monkeypatch):
    fake = use(monkeypatch, FakeProvider({"c0.mp3": ("", []), "c1.mp3": ("", [])}))
    t = run(tr.transcribe_chunks(INFO, chunks(2), "auto"))
    assert [lang for _, lang in fake.calls] == [None, None]
    assert t.language == "und"


def test_explicit_language_is_used_everywhere(monkeypatch):
    fake = use(monkeypatch, FakeProvider(empty_results(3)))
    t = run(tr.transcribe_chunks(INFO, chunks(3), "pt-BR"))
    assert {lang for _, lang in fake.calls} == {"pt"}
    assert t.language == "pt"


def test_progress_increases_to_one(monkeypatch):
    use(monkeypatch, FakeProvider(empty_results(4)))
    progress: list[float] = []
    run(tr.transcribe_chunks(INFO, chunks(4), None, progress.append))
    assert progress == pytest.approx([0.25, 0.5, 0.75, 1.0])


def test_progress_callback_errors_do_not_break_transcription(monkeypatch):
    use(monkeypatch, FakeProvider(empty_results(2)))

    def broken(_fraction: float) -> None:
        raise RuntimeError("ui bug")

    assert run(tr.transcribe_chunks(INFO, chunks(2), "en", broken)).segments == []


def test_no_chunks_gives_empty_transcript(monkeypatch):
    use(monkeypatch, FakeProvider({}))
    progress: list[float] = []
    t = run(tr.transcribe_chunks(INFO, [], None, progress.append))
    assert t.segments == [] and t.language == "und" and progress == [1.0]


def test_definitive_failure_cancels_pending_chunks(monkeypatch):
    boom = AppError("PROVIDER_ERROR", "boom", http_status=502)
    fake = use(monkeypatch, FakeProvider(
        empty_results(3), delays={"c2.mp3": 5.0}, fail={"c1.mp3": boom},
    ))
    started = time.monotonic()
    with pytest.raises(AppError) as exc:
        run(tr.transcribe_chunks(INFO, chunks(3), "en"))
    assert exc.value is boom
    assert fake.cancelled == ["c2.mp3"]
    assert time.monotonic() - started < 2


def test_outer_cancellation_cancels_every_chunk(monkeypatch):
    fake = use(monkeypatch, FakeProvider(empty_results(3), delays={f"c{i}.mp3": 5.0 for i in range(3)}))

    async def scenario():
        await asyncio.wait_for(tr.transcribe_chunks(INFO, chunks(3), "en"), timeout=0.1)

    with pytest.raises(TimeoutError):
        run(scenario())
    assert sorted(fake.cancelled) == ["c0.mp3", "c1.mp3", "c2.mp3"]


def test_provider_concurrency_is_limited(monkeypatch):
    monkeypatch.setattr(tr, "settings", make_settings(provider_concurrency=2))
    fake = use(monkeypatch, FakeProvider(empty_results(6), delays={f"c{i}.mp3": 0.02 for i in range(6)}))
    run(tr.transcribe_chunks(INFO, chunks(6), "en"))
    assert fake.max_active == 2


def test_unexpected_provider_exception_becomes_internal_without_leaking(monkeypatch, caplog):
    use(monkeypatch, FakeProvider(empty_results(1), fail={"c0.mp3": ValueError("secret transcript text")}))
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(AppError) as exc:
            run(tr.transcribe_chunks(INFO, chunks(1), "en"))
    assert exc.value.code == "INTERNAL" and exc.value.http_status == 500
    assert "secret transcript text" not in exc.value.message
    assert "secret transcript text" not in caplog.text


def test_end_to_end_with_groq_mock_transport(monkeypatch, tmp_path):
    """transcribe_chunks + GroqProvider together: language detected on chunk 0, then pinned."""
    files = []
    for i in range(2):
        p = tmp_path / f"c{i}.mp3"
        p.write_bytes(AUDIO_BYTES)
        files.append(Chunk(path=str(p), offset_s=i * 600.0, duration_s=600.0))
    bodies: list[bytes] = []

    def handler(request):
        bodies.append(request.content)
        return httpx.Response(200, json={"language": "spanish", "segments": [{"start": 1.0, "end": 2.0, "text": " hola"}]})

    monkeypatch.setattr(tr, "get_provider", lambda: groq(handler))
    t = run(tr.transcribe_chunks(INFO, files, None))
    assert [(s.start, s.text) for s in t.segments] == [(1.0, "hola"), (601.0, "hola")]
    assert t.language == "es"
    assert b'name="language"' not in bodies[0] and b'name="language"\r\n\r\nes\r\n' in bodies[1]


# ------------------------------------------------------------------ cost

def test_estimate_cost_usd():
    assert tr.estimate_cost_usd(3600) == pytest.approx(0.04)
    assert tr.estimate_cost_usd(7200) == pytest.approx(0.08)
    assert tr.estimate_cost_usd(5) == pytest.approx(5 / 3600 * 0.04)
    assert tr.estimate_cost_usd(0) == 0.0
    assert tr.estimate_cost_usd(1801) == pytest.approx(1801 / 3600 * 0.04)
    assert tr.estimate_cost_usd(1800) == pytest.approx(1800 / 3600 * 0.04)
