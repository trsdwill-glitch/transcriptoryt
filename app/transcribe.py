"""Transcription through a hosted Whisper API (Groq by default, Cloudflare Workers AI as alternative).

Privacy: API keys, response bodies and transcribed text are never logged or returned in errors.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import math
import re
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from app.config import settings
from app.models import AppError, AudioInfo, Chunk, Segment, Transcript

log = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
CF_MODEL = "@cf/openai/whisper-large-v3-turbo"
CF_URL = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/" + CF_MODEL

REQUEST_TIMEOUT_S = 180.0
CONNECT_TIMEOUT_S = 20.0
RETRY_DELAYS_S: tuple[float, ...] = (1.0, 3.0, 8.0)  # waits before retries 1, 2 and 3
MAX_RETRY_AFTER_S = 60.0  # a longer Retry-After means an exhausted quota window: fail fast

# Whisper language names -> ISO 639-1 codes (providers may answer with either form).
_LANGUAGE_PAIRS = (
    "en:english zh:chinese de:german es:spanish ru:russian ko:korean fr:french ja:japanese pt:portuguese "
    "tr:turkish pl:polish ca:catalan nl:dutch ar:arabic sv:swedish it:italian id:indonesian hi:hindi "
    "fi:finnish vi:vietnamese he:hebrew uk:ukrainian el:greek ms:malay cs:czech ro:romanian da:danish "
    "hu:hungarian ta:tamil no:norwegian th:thai ur:urdu hr:croatian bg:bulgarian lt:lithuanian la:latin "
    "mi:maori ml:malayalam cy:welsh sk:slovak te:telugu fa:persian lv:latvian bn:bengali sr:serbian "
    "az:azerbaijani sl:slovenian kn:kannada et:estonian mk:macedonian br:breton eu:basque is:icelandic "
    "hy:armenian ne:nepali mn:mongolian bs:bosnian kk:kazakh sq:albanian sw:swahili gl:galician "
    "mr:marathi pa:punjabi si:sinhala km:khmer sn:shona yo:yoruba so:somali af:afrikaans oc:occitan "
    "ka:georgian be:belarusian tg:tajik sd:sindhi gu:gujarati am:amharic yi:yiddish lo:lao uz:uzbek "
    "fo:faroese ht:haitian_creole ps:pashto tk:turkmen nn:nynorsk mt:maltese sa:sanskrit "
    "lb:luxembourgish my:myanmar bo:tibetan tl:tagalog mg:malagasy as:assamese tt:tatar haw:hawaiian "
    "ln:lingala ha:hausa ba:bashkir jw:javanese su:sundanese yue:cantonese"
)
_LANGUAGE_NAMES: dict[str, str] = {
    name.replace("_", " "): code for code, name in (p.split(":") for p in _LANGUAGE_PAIRS.split())
}
_LANGUAGE_NAMES.update(
    castilian="es", valencian="ca", flemish="nl", burmese="my", mandarin="zh", pushto="ps",
    panjabi="pa", sinhalese="si", moldavian="ro", moldovan="ro", letzeburgesch="lb", haitian="ht",
)
_CODE_RE = re.compile(r"[a-z]{2,3}")


class Provider(Protocol):
    async def transcribe(self, chunk_path: str, language: str | None) -> tuple[str, list[Segment]]:
        """Return (ISO language code or '' if unknown, segments with times relative to the chunk)."""


# ---------------------------------------------------------------- helpers

def _provider_error(message: str) -> AppError:
    return AppError("PROVIDER_ERROR", message, http_status=502)


def _normalize_language(value: object) -> str:
    """Map a provider language (name or code) to a lowercase ISO code; '' when unknown."""
    if not isinstance(value, str):
        return ""
    raw = value.strip().lower().replace("_", "-")
    if raw in _LANGUAGE_NAMES:
        return _LANGUAGE_NAMES[raw]
    base = raw.split("-")[0]
    return base if _CODE_RE.fullmatch(base) else ""


def _requested_language(language: str | None) -> str | None:
    """User choice -> ISO code, or None for auto-detection ('auto', '' and None)."""
    if language is None:
        return None
    code = language.strip().lower().replace("_", "-").split("-")[0]
    return None if code in ("", "auto") else code


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _parse_segments(raw: object) -> list[Segment]:
    """Segments (times relative to the chunk) from a provider's `segments` list; bad items are skipped."""
    out: list[Segment] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            continue
        start = _as_float(item.get("start"))
        if start is None:
            continue
        start = max(0.0, start)
        end = _as_float(item.get("end"))
        out.append(Segment(start, start if end is None or end < start else end, item["text"].strip()))
    return out


def _parse_retry_after(value: str | None) -> float | None:
    """Retry-After as seconds (number or HTTP date); None when absent or invalid."""
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, seconds) if math.isfinite(seconds) else None


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


async def _read_audio(path: str) -> bytes:
    try:
        data = await asyncio.to_thread(Path(path).read_bytes)
    except OSError:
        raise AppError("INTERNAL", "Audio chunk could not be read.", http_status=500) from None
    if not data:
        raise AppError("INTERNAL", "Audio chunk is empty.", http_status=500)
    return data


def _client(transport: httpx.AsyncBaseTransport | None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(REQUEST_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
    )


def _json_object(resp: httpx.Response) -> dict[str, Any]:
    try:
        payload = resp.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        log.error("provider_bad_response status=%d", resp.status_code)
        raise _provider_error("The transcription service returned an unexpected response.")
    return payload


async def _post_with_retries(client: httpx.AsyncClient, url: str, *, provider: str, **kwargs: Any) -> httpx.Response:
    """POST with backoff on 429, 5xx and network errors. Returns a 2xx response or raises PROVIDER_ERROR."""
    attempts = len(RETRY_DELAYS_S) + 1
    for attempt in range(attempts):
        retry_after: float | None = None
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_S):
                resp = await client.post(url, **kwargs)
        except (httpx.RequestError, TimeoutError) as exc:
            log.warning("provider=%s network_error=%s attempt=%d/%d", provider, type(exc).__name__, attempt + 1, attempts)
        else:
            status = resp.status_code
            if 200 <= status < 300:
                return resp
            if status != 429 and status < 500:
                # 401/403 (bad credentials) and other 4xx cannot succeed on a retry
                log.error("provider=%s rejected status=%d", provider, status)
                raise _provider_error("The transcription service rejected the request.")
            log.warning("provider=%s status=%d attempt=%d/%d", provider, status, attempt + 1, attempts)
            if status == 429:
                retry_after = _parse_retry_after(resp.headers.get("retry-after"))
        if attempt == attempts - 1:
            break
        delay = RETRY_DELAYS_S[attempt]
        if retry_after is not None:
            if retry_after > MAX_RETRY_AFTER_S:
                log.error("provider=%s retry_after_too_long seconds=%.0f", provider, retry_after)
                break
            delay = max(delay, retry_after)
        await _sleep(delay)
    raise _provider_error("The transcription service is unavailable. Please try again later.")


# ---------------------------------------------------------------- providers

class GroqProvider:
    """Groq's OpenAI-compatible Whisper endpoint (verbose_json with segment timestamps)."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        key = (settings.groq_api_key if api_key is None else api_key).strip()
        if not key:
            log.error("provider=groq is not configured")
            raise AppError("INTERNAL", "The transcription service is not configured.", http_status=500)
        self._api_key = key
        self._model = model or settings.groq_model
        self._transport = transport

    async def transcribe(self, chunk_path: str, language: str | None) -> tuple[str, list[Segment]]:
        audio = await _read_audio(chunk_path)
        form = {
            "model": self._model,
            "response_format": "verbose_json",
            "timestamp_granularities[]": "segment",
            "temperature": "0",
        }
        if language:
            form["language"] = language
        async with _client(self._transport) as client:
            resp = await _post_with_retries(
                client,
                GROQ_URL,
                provider="groq",
                headers={"Authorization": f"Bearer {self._api_key}"},
                data=form,
                files={"file": ("audio.mp3", audio, "audio/mpeg")},
            )
        payload = _json_object(resp)
        segments = _parse_segments(payload.get("segments"))
        text = payload.get("text")
        if "segments" not in payload and isinstance(text, str) and text.strip():
            segments = [Segment(0.0, _as_float(payload.get("duration")) or 0.0, text.strip())]
        return _normalize_language(payload.get("language")), segments


class CloudflareProvider:
    """Workers AI REST: @cf/openai/whisper-large-v3-turbo with a base64 `audio` field in a JSON body."""

    def __init__(
        self,
        account_id: str | None = None,
        api_token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        account = (settings.cf_account_id if account_id is None else account_id).strip()
        token = (settings.cf_api_token if api_token is None else api_token).strip()
        if not account or not token:
            log.error("provider=cloudflare is not configured")
            raise AppError("INTERNAL", "The transcription service is not configured.", http_status=500)
        self._url = CF_URL.format(account_id=quote(account, safe=""))
        self._token = token
        self._transport = transport

    async def transcribe(self, chunk_path: str, language: str | None) -> tuple[str, list[Segment]]:
        audio = await _read_audio(chunk_path)
        body: dict[str, Any] = {"audio": base64.b64encode(audio).decode("ascii")}
        if language:
            body["language"] = language  # omitted -> the model auto-detects
        async with _client(self._transport) as client:
            resp = await _post_with_retries(
                client,
                self._url,
                provider="cloudflare",
                headers={"Authorization": f"Bearer {self._token}"},
                json=body,
            )
        payload = _json_object(resp)
        if payload.get("success") is False:
            log.error("provider=cloudflare success=false")
            raise _provider_error("The transcription service reported an error.")
        result = payload.get("result")
        if not isinstance(result, dict):
            result = payload  # tolerate an unwrapped response
        info = result.get("transcription_info")
        info = info if isinstance(info, dict) else {}
        segments = _parse_segments(result.get("segments"))
        text = result.get("text")
        if "segments" not in result and isinstance(text, str) and text.strip():
            segments = [Segment(0.0, _as_float(info.get("duration")) or 0.0, text.strip())]
        return _normalize_language(info.get("language")), segments


def get_provider() -> Provider:
    """Provider selected by settings.provider ('groq' or 'cloudflare')."""
    name = (settings.provider or "").strip().lower()
    if name == "groq":
        return GroqProvider()
    if name == "cloudflare":
        return CloudflareProvider()
    log.error("unknown provider configured")
    raise AppError("INTERNAL", "The transcription service is not configured.", http_status=500)


# ---------------------------------------------------------------- orchestration

def _report(callback: Callable[[float], None] | None, fraction: float) -> None:
    if callback is None:
        return
    try:
        callback(min(1.0, max(0.0, fraction)))
    except Exception as exc:  # a broken progress hook must not fail the transcription
        log.warning("progress_callback_failed type=%s", type(exc).__name__)


def _shift(segments: list[Segment], offset_s: float) -> list[Segment]:
    """Chunk-relative segments -> absolute times; empty or whitespace-only segments are dropped."""
    out: list[Segment] = []
    for seg in segments:
        text = seg.text.strip()
        if text:
            out.append(Segment(seg.start + offset_s, max(seg.end, seg.start) + offset_s, text))
    return out


async def transcribe_chunks(
    info: AudioInfo,
    chunks: list[Chunk],
    language: str | None,
    on_progress: Callable[[float], None] | None = None,
) -> Transcript:
    """Transcribe all chunks (at most settings.provider_concurrency at once) into one Transcript.

    language None/'auto': detected on the first chunk and reused for the rest to avoid language flips.
    """
    requested = _requested_language(language)
    provider = get_provider()
    total = len(chunks)
    gate = asyncio.Semaphore(max(1, int(settings.provider_concurrency)))
    parts: list[list[Segment]] = [[] for _ in chunks]
    finished = 0

    async def run(index: int, lang: str | None) -> str:
        nonlocal finished
        chunk = chunks[index]
        try:
            async with gate:
                detected, segments = await provider.transcribe(chunk.path, lang)
        except AppError:
            raise
        except Exception as exc:  # a provider bug must not leak transcript text through its message
            log.error("transcribe_unexpected_error type=%s", type(exc).__name__)
            raise AppError("INTERNAL", "Unexpected error while transcribing.", http_status=500) from None
        parts[index] = _shift(segments, chunk.offset_s)
        finished += 1
        _report(on_progress, finished / total)
        return detected

    detected = ""
    tasks: list[asyncio.Task[str]] = []
    try:
        lang, first = requested, 0
        if requested is None and chunks:
            detected = await run(0, None)
            lang, first = detected or None, 1
        tasks = [asyncio.create_task(run(i, lang)) for i in range(first, total)]
        if tasks:
            await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:  # a definitive failure (or a cancelled job) stops the pending chunks
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    if total == 0:
        _report(on_progress, 1.0)
    segments = sorted((s for part in parts for s in part), key=lambda s: (s.start, s.end))
    final_language = requested or detected or "und"
    log.info("transcribed chunks=%d segments=%d language=%s", total, len(segments), final_language)
    return Transcript(
        title=info.title,
        source=info.source,
        language=final_language,
        duration_s=info.duration_s,
        segments=segments,
    )


def estimate_cost_usd(duration_s: float) -> float:
    """Estimate cost using the shared contract's duration-based formula."""
    return duration_s / 3600 * settings.cost_per_hour_usd
