"""Audio ingestion (role B): direct downloads, yt-dlp, uploads, ffprobe and ffmpeg chunking.

Privacy: logs carry metadata only (sizes, durations, error codes). URLs, titles, file
names and raw tool output never reach the logs or the user-facing messages.

DNS-rebinding defence for direct downloads
------------------------------------------
Every hop (first request and each redirect) is validated with `validate_url` and
`assert_public_host`, and the connection is then made to the *already validated IP*:
the request URL carries the IP, the `Host` header carries the real host and the httpx
`sni_hostname` request extension makes TLS use (and verify the certificate against) the
real host name. httpx therefore never resolves the name a second time. This works with
stock httpx (no custom transport) and keeps normal certificate validation.
"""
from __future__ import annotations

import asyncio
import functools
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import shutil
import signal
import tempfile
from collections.abc import Awaitable, Callable
from typing import Any, Optional, ParamSpec, TypeVar
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import httpx

from app.config import settings
from app.models import AppError, AudioInfo, Chunk, RemoteProbe
from app.security import assert_public_host, is_ytdlp_url, validate_url

__all__ = ["probe_url", "fetch_audio", "save_upload", "probe_file", "make_chunks"]

log = logging.getLogger("app.ingest")
# httpx logs every request URL at INFO level; keep user URLs out of the logs.
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

P = ParamSpec("P")
R = TypeVar("R")

# ---------------------------------------------------------------- limits
DOWNLOAD_TIMEOUT_S = 120.0        # total time for one direct download
PROBE_TIMEOUT_S = 20.0            # total time for the header-only probe of a direct link
MAX_REDIRECTS = 3
MAX_IP_ATTEMPTS = 4
READ_SIZE = 256 * 1024
YTDLP_PROBE_TIMEOUT_S = 90.0
YTDLP_DOWNLOAD_TIMEOUT_S = 300.0
FFPROBE_TIMEOUT_S = 30.0
DECODE_TIMEOUT_S = 120.0
MIN_CHUNK_S = 0.5

_USER_AGENT = "Mozilla/5.0 (compatible; AudioTranscriber/1.0)"
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_OTHER_MEDIA_TYPES = frozenset(
    {"application/octet-stream", "binary/octet-stream", "application/ogg", "application/mp4"}
)

# Test hook: replace with an httpx.MockTransport without touching the client options.
_TRANSPORT: Optional[httpx.AsyncBaseTransport] = None

# ffmpeg/ffprobe only read containers from this list and only open local files. Without it a
# crafted upload (HLS .m3u8, ffconcat script, ...) could make ffmpeg fetch internal URLs or
# read other local files.
_DEMUXERS = (
    "mov", "matroska", "webm", "ogg", "mp3", "wav", "flac", "aac", "ac3", "eac3", "aiff",
    "amr", "amrnb", "amrwb", "asf", "avi", "flv", "mpegts", "mpeg", "caf", "ape", "wv",
    "dts", "dtshd", "truehd", "au", "w64", "voc", "mpc", "mpc8", "tta", "tak", "rm",
)
_INPUT_GUARD = ["-protocol_whitelist", "file", "-format_whitelist", ",".join(_DEMUXERS)]

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_EXT_RE = re.compile(r"\.[a-z0-9]{1,5}")
_CLOCK_RE = re.compile(r"(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")
_COLLECTION_PATH_RE = re.compile(
    r"^/(?:playlist|channel/|c/|user/|@|feed/|results|hashtag/|podcasts|browse)", re.IGNORECASE
)
_PARTIAL_SUFFIXES = (".json", ".part", ".ytdl", ".temp", ".tmp")
_UNAVAILABLE_TOKENS = (
    "video unavailable", "private video", "this video is private", "members-only", "members only",
    "join this channel", "this video is not available", "has been removed", "no longer available",
    "has been terminated", "is not available in your country", "not made this video available",
    "blocked it in your country", "live event will begin", "premieres in", "this live event",
    "copyright",
)


# ---------------------------------------------------------------- small helpers


def _guarded(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Known failures stay AppError; anything else becomes DOWNLOAD_FAILED without details."""

    @functools.wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await func(*args, **kwargs)
        except AppError:
            raise
        except Exception as exc:  # noqa: BLE001 - deliberate catch-all, details are not exposed
            log.warning("%s failed unexpectedly (%s)", func.__name__, type(exc).__name__)
            raise AppError("DOWNLOAD_FAILED", "The file could not be downloaded or processed.") from None

    return wrapper


def _clean_text(raw: object, fallback: str, limit: int = 200) -> str:
    text = " ".join(_CONTROL_RE.sub(" ", str(raw or "")).split())
    return text[:limit] or fallback


def _basename(name: str) -> str:
    return (name or "").replace("\\", "/").rsplit("/", 1)[-1]


def _safe_ext(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    return ext if _EXT_RE.fullmatch(ext) else ".bin"


def _silent_remove(path: Optional[str]) -> None:
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass


def _to_float(value: object) -> Optional[float]:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _max_download_bytes() -> int:
    return int(float(settings.max_download_mb) * 1024 * 1024)


def _max_file_s() -> float:
    return float(settings.max_file_min) * 60.0


def _chunk_seconds() -> float:
    value = _to_float(getattr(settings, "chunk_seconds", 600))
    return value if value is not None and value >= 1 else 600.0


def _too_long() -> AppError:
    return AppError(
        "TOO_LONG",
        f"The audio is longer than {settings.max_file_min} minutes.",
        {"max_min": settings.max_file_min},
    )


def _too_large(max_mb: Any) -> AppError:
    return AppError("TOO_LARGE", f"The file is larger than {max_mb} MB.", {"max_mb": max_mb})


def _url_name(url: str) -> str:
    return unquote(urlsplit(url).path.rsplit("/", 1)[-1])


def _title_from_url(url: str) -> str:
    return _clean_text(os.path.splitext(_url_name(url))[0], "audio")


# ---------------------------------------------------------------- subprocesses


def _kill(proc: asyncio.subprocess.Process) -> None:
    """Kill the child and, where possible, its whole process group (yt-dlp spawns ffmpeg)."""
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # child runs in its own session: pgid == pid
    except (AttributeError, ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass


async def _run(
    cmd: list[str], *, timeout: float, tool: str, timeout_code: str = "TIMEOUT"
) -> tuple[int, bytes, bytes]:
    """Run a command without a shell. Never raises tool output; kills the child on timeout."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError:
        raise AppError("INTERNAL", f"{tool} is not installed on the server.", http_status=500) from None
    except OSError:
        raise AppError("INTERNAL", f"{tool} could not be started on the server.", http_status=500) from None
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        _kill(proc)
        await proc.wait()
        raise AppError(timeout_code, f"{tool} took too long and was stopped.") from None
    except BaseException:
        _kill(proc)  # cancellation or any other failure: never leave the child running
        await proc.wait()  # reap it, so there is no zombie and no half-open transport
        raise
    return (proc.returncode if proc.returncode is not None else -1), stdout, stderr


# ---------------------------------------------------------------- ffprobe / ffmpeg


async def _ffprobe_json(path: str) -> dict[str, Any]:
    cmd = [
        "ffprobe", "-v", "error", *_INPUT_GUARD,
        "-show_entries", "format=duration", "-show_entries", "stream=codec_type",
        "-of", "json", path,
    ]
    rc, out, _err = await _run(cmd, timeout=FFPROBE_TIMEOUT_S, tool="ffprobe", timeout_code="UNSUPPORTED_FILE")
    if rc != 0:
        raise AppError("UNSUPPORTED_FILE", "The file is not a valid audio or video file.")
    try:
        data = json.loads(out)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise AppError("UNSUPPORTED_FILE", "The file is not a valid audio or video file.")
    return data


async def _decoded_duration(path: str) -> float:
    """Measure the duration by decoding; for containers that carry none (e.g. browser WebM)."""
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", *_INPUT_GUARD,
        "-progress", "pipe:1", "-nostats", "-i", path,
        "-map", "0:a:0", "-vn", "-sn", "-dn", "-f", "null", "-",
    ]
    rc, out, _err = await _run(cmd, timeout=DECODE_TIMEOUT_S, tool="ffmpeg", timeout_code="UNSUPPORTED_FILE")
    if rc != 0:
        raise AppError("UNSUPPORTED_FILE", "The audio could not be read.")
    best = 0.0
    for match in _CLOCK_RE.finditer(out.decode("utf-8", errors="replace")):
        hours, minutes, seconds = match.groups()
        best = max(best, int(hours) * 3600 + int(minutes) * 60 + float(seconds))
    return best


@_guarded
async def probe_file(path: str) -> float:
    """Duration in seconds; UNSUPPORTED_FILE unless there is an audio stream and duration > 0."""
    data = await _ffprobe_json(path)
    streams = data.get("streams")
    has_audio = isinstance(streams, list) and any(
        isinstance(item, dict) and item.get("codec_type") == "audio" for item in streams
    )
    if not has_audio:
        raise AppError("UNSUPPORTED_FILE", "The file does not contain an audio track.")
    fmt = data.get("format")
    duration = _to_float(fmt.get("duration")) if isinstance(fmt, dict) else None
    if duration is None or duration <= 0:
        duration = await _decoded_duration(path)
    if duration <= 0:
        raise AppError("UNSUPPORTED_FILE", "The audio track is empty.")
    return duration


def _read_segment_starts(list_path: str) -> dict[str, float]:
    """Exact start of each segment (in the original timeline) from ffmpeg's CSV segment list."""
    starts: dict[str, float] = {}
    try:
        with open(list_path, "r", encoding="utf-8") as handle:
            for line in handle:
                parts = line.strip().split(",")
                if len(parts) >= 3:
                    start = _to_float(parts[1])
                    if start is not None and start >= 0:
                        starts[os.path.basename(parts[0])] = start
    except OSError:
        pass
    return starts


@_guarded
async def make_chunks(info: AudioInfo, workdir: str) -> list[Chunk]:
    """Mono 16 kHz MP3 (64 kbps) pieces of settings.chunk_seconds, no overlap."""
    chunk_s = _chunk_seconds()
    os.makedirs(workdir, exist_ok=True)
    out_dir = tempfile.mkdtemp(prefix="chunks_", dir=workdir)
    list_path = os.path.join(out_dir, "segments.csv")
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        *_INPUT_GUARD, "-i", info.path,
        "-map", "0:a:0", "-vn", "-sn", "-dn", "-map_metadata", "-1",
        "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "64k",
        "-f", "segment", "-segment_time", f"{chunk_s:g}", "-segment_format", "mp3",
        "-reset_timestamps", "1", "-segment_list", list_path, "-segment_list_type", "csv",
        os.path.join(out_dir.replace("%", "%%"), "chunk_%04d.mp3"),  # "%%" = literal % for ffmpeg
    ]
    timeout = max(120.0, min(1500.0, 60.0 + float(info.duration_s) * 0.5))
    try:
        rc, _out, err = await _run(cmd, timeout=timeout, tool="ffmpeg")
        if rc != 0:
            if b"no space left" in err.lower():
                raise AppError("INTERNAL", "The server ran out of disk space.", http_status=500)
            raise AppError("UNSUPPORTED_FILE", "The audio could not be converted.")
        starts = _read_segment_starts(list_path)
        names = sorted(n for n in os.listdir(out_dir) if n.startswith("chunk_") and n.endswith(".mp3"))
        chunks: list[Chunk] = []
        for index, name in enumerate(names):
            path = os.path.join(out_dir, name)
            try:
                duration = await probe_file(path)
            except AppError as exc:
                if exc.code == "INTERNAL":
                    raise
                duration = 0.0
            if duration < MIN_CHUNK_S:
                _silent_remove(path)
                continue
            offset = starts.get(name, index * chunk_s)
            if chunks and offset < chunks[-1].offset_s:
                offset = chunks[-1].offset_s + chunks[-1].duration_s
            chunks.append(Chunk(path=path, offset_s=round(offset, 3), duration_s=round(duration, 3)))
        if not chunks:
            raise AppError("UNSUPPORTED_FILE", "No audio could be extracted from the file.")
        _silent_remove(list_path)
        log.info("chunks ready count=%d total_s=%.1f", len(chunks), sum(c.duration_s for c in chunks))
        return chunks
    except BaseException:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise


# ---------------------------------------------------------------- uploads


@_guarded
async def save_upload(tmp_path: str, original_name: str, workdir: str) -> AudioInfo:
    """Move an uploaded temp file into workdir under a generated name and validate it."""
    os.makedirs(workdir, exist_ok=True)
    base = _basename(original_name)
    max_mb = settings.max_upload_mb
    if os.path.getsize(tmp_path) > float(max_mb) * 1024 * 1024:
        raise _too_large(max_mb)
    # The original name never reaches the path; only a sanitized extension is kept.
    dest = os.path.join(workdir, f"up_{secrets.token_hex(8)}{_safe_ext(base)}")
    try:
        await asyncio.to_thread(shutil.move, tmp_path, dest)
        duration = await probe_file(dest)
        if duration > _max_file_s():
            raise _too_long()
    except BaseException:
        _silent_remove(dest)
        raise
    log.info("upload accepted duration_s=%.1f", duration)
    return AudioInfo(
        path=dest,
        duration_s=duration,
        title=_clean_text(os.path.splitext(base)[0], "audio"),
        source=_clean_text(base, "upload", 255),
    )


# ---------------------------------------------------------------- direct links


def _new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=_TRANSPORT,
        follow_redirects=False,  # redirects are followed by hand, each hop re-validated
        trust_env=False,         # ignore proxy/netrc/CA environment variables
        timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "audio/*, video/*;q=0.9, */*;q=0.5",
            "Accept-Encoding": "identity",  # media is not compressible; no decompression bombs
        },
        limits=httpx.Limits(max_connections=2, max_keepalive_connections=0),
    )


async def _send_pinned(client: httpx.AsyncClient, url: str, ips: list[str]) -> httpx.Response:
    """Open a streaming GET to one of the validated IPs (IPv4 first); see module docstring."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    literal = _is_ip_literal(host)
    last_error: Optional[Exception] = None
    for ip in sorted(ips, key=lambda value: ":" in value)[:MAX_IP_ATTEMPTS]:
        netloc = f"[{ip}]" if ":" in ip else ip
        if parts.port is not None:
            netloc = f"{netloc}:{parts.port}"
        pinned = urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))
        request = client.build_request(
            "GET",
            pinned,
            headers={"Host": parts.netloc},
            extensions={} if literal else {"sni_hostname": host},
        )
        try:
            return await client.send(request, stream=True, follow_redirects=False)
        except httpx.HTTPError as exc:
            last_error = exc
    if isinstance(last_error, httpx.TimeoutException):
        raise AppError("TIMEOUT", "The server took too long to respond.")
    raise AppError("DOWNLOAD_FAILED", "Could not connect to the server.")


async def _open_direct(client: httpx.AsyncClient, url: str) -> tuple[httpx.Response, str]:
    """Streaming GET following up to MAX_REDIRECTS redirects by hand.

    Each destination is validated and pinned to a verified public IP. The caller must
    close the returned response. Returns (response, final URL).
    """
    current = validate_url(url)
    for hop in range(MAX_REDIRECTS + 1):
        ips = await assert_public_host(urlsplit(current).hostname or "")
        response = await _send_pinned(client, current, ips)
        if response.status_code not in _REDIRECT_CODES:
            return response, current
        location = response.headers.get("location")
        await response.aclose()
        if not location:
            raise AppError("DOWNLOAD_FAILED", "The server sent an invalid redirect.")
        if hop == MAX_REDIRECTS:
            break
        try:
            current = validate_url(urljoin(current, location))
        except AppError as exc:
            if exc.code == "URL_BLOCKED":
                raise
            raise AppError("DOWNLOAD_FAILED", "The server redirected to an invalid address.") from None
    raise AppError("DOWNLOAD_FAILED", "The link redirected too many times.")


def _check_direct_response(response: httpx.Response, max_bytes: int) -> None:
    status = response.status_code
    if status in (404, 410):
        raise AppError("UNAVAILABLE", "The file could not be found at that link.")
    if not 200 <= status < 300:
        raise AppError("DOWNLOAD_FAILED", f"The server answered with HTTP {status}.")
    ctype = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if not (ctype.startswith(("audio/", "video/")) or ctype in _OTHER_MEDIA_TYPES):
        raise AppError("UNSUPPORTED_FILE", "That link does not point to an audio or video file.")
    length = _to_float(response.headers.get("content-length"))
    if length is not None and length > max_bytes:
        raise _too_large(settings.max_download_mb)


async def _probe_direct(url: str) -> RemoteProbe:
    """Header-only check of a direct link: reachable, right type, not too large."""
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_S):
            async with _new_client() as client:
                response, final_url = await _open_direct(client, url)
                try:
                    _check_direct_response(response, _max_download_bytes())
                finally:
                    await response.aclose()
    except (TimeoutError, httpx.TimeoutException):
        raise AppError("TIMEOUT", "The server took too long to respond.") from None
    except httpx.HTTPError:
        raise AppError("DOWNLOAD_FAILED", "Could not read the file at that link.") from None
    return RemoteProbe(title=_title_from_url(final_url), duration_s=None, kind="direct")


async def _download_direct(url: str, source: str, workdir: str) -> AudioInfo:
    max_bytes = _max_download_bytes()
    dest: Optional[str] = None
    written = 0
    try:
        async with asyncio.timeout(DOWNLOAD_TIMEOUT_S):
            async with _new_client() as client:
                response, final_url = await _open_direct(client, url)
                try:
                    _check_direct_response(response, max_bytes)
                    dest = os.path.join(workdir, f"dl_{secrets.token_hex(8)}{_safe_ext(_url_name(final_url))}")
                    with open(dest, "wb") as handle:
                        async for block in response.aiter_raw(READ_SIZE):
                            written += len(block)
                            if written > max_bytes:
                                raise _too_large(settings.max_download_mb)
                            await asyncio.to_thread(handle.write, block)
                finally:
                    await response.aclose()
        duration = await probe_file(dest)
        if duration > _max_file_s():
            raise _too_long()
    except BaseException as exc:
        _silent_remove(dest)
        if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
            raise AppError("TIMEOUT", "The download took too long and was stopped.") from None
        if isinstance(exc, httpx.HTTPError):
            raise AppError("DOWNLOAD_FAILED", "The download was interrupted.") from None
        raise
    log.info("direct download ok bytes=%d duration_s=%.1f", written, duration)
    return AudioInfo(path=dest, duration_s=duration, title=_title_from_url(final_url), source=source)


# ---------------------------------------------------------------- yt-dlp


def _ytdlp_net_args() -> list[str]:
    args: list[str] = []
    proxy = getattr(settings, "ytdlp_proxy", "")
    if proxy:
        args += ["--proxy", str(proxy)]
    cookies = getattr(settings, "ytdlp_cookies_file", "")
    if cookies:
        args += ["--cookies", str(cookies)]
    return args


def _reject_collection(url: str) -> None:
    """Playlists, channels and search pages are not accepted: single videos only."""
    parts = urlsplit(url)
    if "youtube" in (parts.hostname or "") and _COLLECTION_PATH_RE.match(parts.path):
        raise AppError("URL_INVALID", "Only single videos are supported, not playlists or channels.")


def _ytdlp_error(stderr: bytes) -> AppError:
    """Translate yt-dlp's stderr into a contract error. The text itself is never exposed."""
    text = stderr.decode("utf-8", errors="replace").lower().replace("\u2019", "'")
    if re.search(r"confirm you.{0,3}re not a bot", text):
        code, message = "YOUTUBE_BLOCKED", "YouTube blocked the request. Please try again later."
    elif any(token in text for token in _UNAVAILABLE_TOKENS) or re.search(r"\bage\b", text):
        code, message = "UNAVAILABLE", "This video is private, removed, restricted or not available."
    elif "login_required" in text or "http error 403" in text or "http error 429" in text:
        code, message = "YOUTUBE_BLOCKED", "YouTube blocked the request. Please try again later."
    elif "unsupported url" in text or "is not a valid url" in text or "incomplete youtube id" in text:
        code, message = "URL_INVALID", "That link is not a supported video link."
    else:
        code, message = "DOWNLOAD_FAILED", "The video could not be downloaded."
    log.info("yt-dlp failed code=%s", code)
    return AppError(code, message)


async def _probe_ytdlp(url: str) -> RemoteProbe:
    _reject_collection(url)
    cmd = [
        "yt-dlp", "--dump-single-json", "--no-playlist", "--playlist-items", "1",
        "--skip-download", "--no-warnings", *_ytdlp_net_args(), "--", url,
    ]
    rc, out, err = await _run(cmd, timeout=YTDLP_PROBE_TIMEOUT_S, tool="yt-dlp")
    if rc != 0:
        raise _ytdlp_error(err)
    try:
        data = json.loads(out)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise AppError("DOWNLOAD_FAILED", "The video information could not be read.")
    if data.get("_type") not in (None, "video") or data.get("entries"):
        raise AppError("URL_INVALID", "Only single videos are supported, not playlists or channels.")
    if data.get("is_live") or data.get("live_status") in ("is_live", "is_upcoming", "post_live"):
        raise AppError("UNAVAILABLE", "Live and scheduled videos cannot be transcribed.")
    if data.get("availability") in ("private", "premium_only", "subscriber_only"):
        raise AppError("UNAVAILABLE", "This video is private or restricted.")
    duration = _to_float(data.get("duration"))
    if duration is not None and duration <= 0:
        duration = None
    if duration is not None and duration > _max_file_s():
        raise _too_long()
    title = _clean_text(data.get("title") or data.get("fulltitle") or data.get("id"), "video")
    log.info("probe ok kind=ytdlp duration_s=%s", "unknown" if duration is None else f"{duration:.1f}")
    return RemoteProbe(title=title, duration_s=duration, kind="ytdlp")


def _pick_media(folder: str) -> Optional[str]:
    """Largest finished media file in `folder` (skips metadata and partial downloads)."""
    best: Optional[tuple[int, str]] = None
    with os.scandir(folder) as entries:
        for entry in entries:
            if not entry.is_file() or entry.name.lower().endswith(_PARTIAL_SUFFIXES):
                continue
            size = entry.stat().st_size
            if size > 0 and (best is None or size > best[0]):
                best = (size, entry.path)
    return best[1] if best else None


def _read_info_title(folder: str) -> Optional[str]:
    try:
        for name in sorted(os.listdir(folder)):
            if name.endswith(".info.json"):
                with open(os.path.join(folder, name), "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if isinstance(data, dict):
                    return data.get("title") or data.get("fulltitle") or data.get("id")
    except (OSError, ValueError):
        pass
    return None


async def _fetch_ytdlp(url: str, source: str, workdir: str) -> AudioInfo:
    _reject_collection(url)
    sub = tempfile.mkdtemp(prefix="yt_", dir=workdir)  # only this download's files live here
    try:
        max_mb = max(1, int(float(settings.max_download_mb)))
        cmd = [
            "yt-dlp", "-f", "bestaudio/best", "--no-playlist", "--playlist-items", "1",
            "--no-write-playlist-metafiles", "--write-info-json",
            "--max-filesize", f"{max_mb}M", "--socket-timeout", "20", "--retries", "2",
            "--no-warnings", "--no-progress",
            "-o", os.path.join(sub.replace("%", "%%"), "%(id)s.%(ext)s"),  # "%%" = literal % for yt-dlp
            *_ytdlp_net_args(), "--", url,
        ]
        rc, out, err = await _run(cmd, timeout=YTDLP_DOWNLOAD_TIMEOUT_S, tool="yt-dlp")
        if rc != 0:
            raise _ytdlp_error(err)
        media = _pick_media(sub)
        if media is None:
            # yt-dlp exits 0 without a file when --max-filesize is exceeded.
            if b"max-filesize" in (out + err).lower():
                raise _too_large(settings.max_download_mb)
            raise AppError("DOWNLOAD_FAILED", "The video could not be downloaded.")
        title = _clean_text(_read_info_title(sub), "video")
        dest = os.path.join(workdir, f"yt_{secrets.token_hex(8)}{_safe_ext(media)}")
        os.replace(media, dest)
        try:
            duration = await probe_file(dest)
            if duration > _max_file_s():
                raise _too_long()
        except BaseException:
            _silent_remove(dest)
            raise
        log.info("ytdlp download ok duration_s=%.1f", duration)
        return AudioInfo(path=dest, duration_s=duration, title=title, source=source)
    finally:
        shutil.rmtree(sub, ignore_errors=True)


# ---------------------------------------------------------------- public entry points


@_guarded
async def probe_url(url: str) -> RemoteProbe:
    """Metadata only, no media download. Direct links have duration_s=None (unknown until fetched)."""
    clean = validate_url(url)
    if is_ytdlp_url(clean):
        return await _probe_ytdlp(clean)
    return await _probe_direct(clean)


@_guarded
async def fetch_audio(url: str, workdir: str) -> AudioInfo:
    """Download one video/audio into workdir and validate it with ffprobe."""
    clean = validate_url(url)
    os.makedirs(workdir, exist_ok=True)
    if is_ytdlp_url(clean):
        return await _fetch_ytdlp(clean, url.strip(), workdir)
    return await _download_direct(clean, url.strip(), workdir)
