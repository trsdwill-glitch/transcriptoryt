"""Tests for app.ingest.

No internet: HTTP goes through httpx.MockTransport, DNS is faked at socket.getaddrinfo and
yt-dlp is a fake executable. Tests that need ffmpeg generate a tone with it and are skipped
when ffmpeg/ffprobe are not installed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Callable

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import ingest, security  # noqa: E402
from app.models import AppError, AudioInfo  # noqa: E402

YTDLP_HOSTS = ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be")
HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe are not installed")
needs_posix = pytest.mark.skipif(os.name != "posix", reason="fake executables need a POSIX system")


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def error_of(coro: Any) -> AppError:
    with pytest.raises(AppError) as exc:
        asyncio.run(coro)
    return exc.value


def make_settings(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = dict(
        max_download_mb=1, max_file_min=120, max_upload_mb=10, chunk_seconds=10,
        ytdlp_proxy="", ytdlp_cookies_file="",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture(autouse=True)
def fixed_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ingest, "settings", make_settings())
    monkeypatch.setattr(security, "settings", SimpleNamespace(ytdlp_hosts=YTDLP_HOSTS))


def fake_dns(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, list[str]]) -> None:
    def _getaddrinfo(host: str, port: Any, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0) -> list:
        if host not in mapping:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        out = []
        for ip in mapping[host]:
            if ":" in ip:
                out.append((socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, 0, 0, 0)))
            else:
                out.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)))
        return out

    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo)


def use_transport(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], Any]) -> None:
    monkeypatch.setattr(ingest, "_TRANSPORT", httpx.MockTransport(handler))


def audio_response(body: bytes = b"ID3" + b"\x00" * 2048, ctype: str = "audio/mpeg", status: int = 200) -> httpx.Response:
    # `stream=` (not `content=`) keeps the body unread, like a real network response.
    headers = {"content-type": ctype, "content-length": str(len(body))}
    return httpx.Response(status, headers=headers, stream=httpx.ByteStream(body))


def patch_probe_file(monkeypatch: pytest.MonkeyPatch, duration: float = 12.5) -> None:
    async def fake(path: str) -> float:
        assert os.path.exists(path)
        return duration

    monkeypatch.setattr(ingest, "probe_file", fake)


PUBLIC = {"example.com": ["93.184.216.34"]}


# ======================================================= direct downloads (HTTP mocked)


def test_direct_download_pins_the_validated_ip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_dns(monkeypatch, PUBLIC)
    seen: list[httpx.Request] = []
    payload = b"ID3" + os.urandom(4096)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return audio_response(payload)

    use_transport(monkeypatch, handler)
    patch_probe_file(monkeypatch, 12.5)
    url = "https://example.com/Some%20Song.mp3"
    info = run(ingest.fetch_audio(url, str(tmp_path)))
    assert isinstance(info, AudioInfo)
    assert Path(info.path).read_bytes() == payload
    assert (info.title, info.duration_s, info.source) == ("Some Song", 12.5, url)
    assert Path(info.path).parent == tmp_path
    assert "Some" not in Path(info.path).name  # generated name, not the remote one
    assert len(seen) == 1
    request = seen[0]
    assert request.url.host == "93.184.216.34"  # connects to the validated IP, never re-resolves
    assert request.headers["host"] == "example.com"
    assert request.extensions["sni_hostname"] == "example.com"
    assert request.url.raw_path == b"/Some%20Song.mp3"
    assert request.headers["accept-encoding"] == "identity"


def test_direct_download_keeps_custom_port_in_host_header(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_dns(monkeypatch, PUBLIC)
    seen: list[httpx.Request] = []
    use_transport(monkeypatch, lambda request: (seen.append(request), audio_response())[1])
    patch_probe_file(monkeypatch)
    run(ingest.fetch_audio("http://example.com:8080/a.mp3", str(tmp_path)))
    assert seen[0].url.port == 8080 and seen[0].url.host == "93.184.216.34"
    assert seen[0].headers["host"] == "example.com:8080"


def test_direct_download_tries_next_ip_and_prefers_ipv4(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    v6 = "2606:2800:220:1:248:1893:25c8:1946"
    fake_dns(monkeypatch, {"example.com": [v6, "93.184.216.34"]})
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.host)
        if request.url.host == "93.184.216.34":
            raise httpx.ConnectError("refused")
        return audio_response()

    use_transport(monkeypatch, handler)
    patch_probe_file(monkeypatch)
    info = run(ingest.fetch_audio("https://example.com/a.mp3", str(tmp_path)))
    assert attempts == ["93.184.216.34", v6]
    assert Path(info.path).exists()


def test_direct_download_all_ips_fail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_dns(monkeypatch, PUBLIC)

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    use_transport(monkeypatch, refuse)
    assert error_of(ingest.fetch_audio("https://example.com/a.mp3", str(tmp_path))).code == "DOWNLOAD_FAILED"

    def too_slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow")

    use_transport(monkeypatch, too_slow)
    assert error_of(ingest.fetch_audio("https://example.com/a.mp3", str(tmp_path))).code == "TIMEOUT"
    assert list(tmp_path.iterdir()) == []


def test_redirect_to_private_ip_is_blocked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_dns(monkeypatch, PUBLIC)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})

    use_transport(monkeypatch, handler)
    err = error_of(ingest.fetch_audio("https://example.com/a.mp3", str(tmp_path)))
    assert err.code == "URL_BLOCKED"
    assert len(seen) == 1  # the private destination was never contacted
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "location",
    [
        "http://127.0.0.1/x.mp3",
        "http://[::1]/x.mp3",
        "http://2130706433/x.mp3",
        "http://internal.example.net/x.mp3",  # resolves to a private IP below
        "http://example.com:22/x.mp3",
        "http://user:pw@example.com/x.mp3",
    ],
)
def test_redirect_destinations_are_revalidated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, location: str) -> None:
    fake_dns(monkeypatch, {**PUBLIC, "internal.example.net": ["10.0.0.5"]})
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(301, headers={"location": location})

    use_transport(monkeypatch, handler)
    assert error_of(ingest.fetch_audio("https://example.com/a.mp3", str(tmp_path))).code == "URL_BLOCKED"
    assert len(seen) == 1


def test_redirect_to_other_host_is_pinned_again(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_dns(monkeypatch, {**PUBLIC, "cdn.example.org": ["151.101.1.1"]})
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.headers["host"] == "example.com":
            return httpx.Response(302, headers={"location": "https://cdn.example.org/files/b.mp3?token=1"})
        return audio_response()

    use_transport(monkeypatch, handler)
    patch_probe_file(monkeypatch)
    info = run(ingest.fetch_audio("https://example.com/a.mp3", str(tmp_path)))
    assert [r.url.host for r in seen] == ["93.184.216.34", "151.101.1.1"]
    assert seen[1].headers["host"] == "cdn.example.org"
    assert seen[1].extensions["sni_hostname"] == "cdn.example.org"
    assert seen[1].url.query == b"token=1"
    assert info.title == "b"


def redirect_chain(length: int, seen: list[httpx.Request]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        index = int(request.url.path.rsplit("/a", 1)[-1])
        if index < length:
            return httpx.Response(302, headers={"location": f"/a{index + 1}"})
        return audio_response()

    return handler


def test_three_redirects_are_followed_four_are_not(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_dns(monkeypatch, PUBLIC)
    patch_probe_file(monkeypatch)
    seen: list[httpx.Request] = []
    use_transport(monkeypatch, redirect_chain(3, seen))
    run(ingest.fetch_audio("https://example.com/a0", str(tmp_path)))
    assert len(seen) == 4

    seen.clear()
    use_transport(monkeypatch, redirect_chain(4, seen))
    err = error_of(ingest.fetch_audio("https://example.com/a0", str(tmp_path / "other")))
    assert err.code == "DOWNLOAD_FAILED"
    assert len(seen) == 4  # gave up without making a fifth request


def test_bad_redirects(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_dns(monkeypatch, PUBLIC)
    use_transport(monkeypatch, lambda request: httpx.Response(302))  # no Location header
    assert error_of(ingest.fetch_audio("https://example.com/a.mp3", str(tmp_path))).code == "DOWNLOAD_FAILED"
    use_transport(monkeypatch, lambda request: httpx.Response(302, headers={"location": "ftp://example.com/a.mp3"}))
    assert error_of(ingest.fetch_audio("https://example.com/a.mp3", str(tmp_path))).code == "DOWNLOAD_FAILED"


@pytest.mark.parametrize("ctype", ["audio/mpeg", "audio/x-m4a", "video/mp4", "application/octet-stream", "Audio/MPEG; charset=binary"])
def test_media_content_types_are_accepted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ctype: str) -> None:
    fake_dns(monkeypatch, PUBLIC)
    use_transport(monkeypatch, lambda request: audio_response(ctype=ctype))
    patch_probe_file(monkeypatch)
    assert run(ingest.fetch_audio("https://example.com/a.bin", str(tmp_path))).duration_s == 12.5


@pytest.mark.parametrize("ctype", ["text/html; charset=utf-8", "application/json", "image/png", "application/pdf", ""])
def test_other_content_types_are_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ctype: str) -> None:
    fake_dns(monkeypatch, PUBLIC)
    use_transport(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": ctype} if ctype else {}, content=b"<html>"))
    assert error_of(ingest.fetch_audio("https://example.com/a.mp3", str(tmp_path))).code == "UNSUPPORTED_FILE"
    assert list(tmp_path.iterdir()) == []


def test_http_error_statuses(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_dns(monkeypatch, PUBLIC)
    for status, code in [(404, "UNAVAILABLE"), (410, "UNAVAILABLE"), (403, "DOWNLOAD_FAILED"), (500, "DOWNLOAD_FAILED")]:
        use_transport(monkeypatch, lambda request, s=status: httpx.Response(s, headers={"content-type": "audio/mpeg"}))
        assert error_of(ingest.fetch_audio("https://example.com/a.mp3", str(tmp_path))).code == code


def test_content_length_over_the_limit_is_rejected_before_reading(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_dns(monkeypatch, PUBLIC)
    use_transport(monkeypatch, lambda request: audio_response(b"x" * (2 * 1024 * 1024)))
    err = error_of(ingest.fetch_audio("https://example.com/big.mp3", str(tmp_path)))
    assert (err.code, err.params) == ("TOO_LARGE", {"max_mb": 1})
    assert list(tmp_path.iterdir()) == []


def test_streamed_body_over_the_limit_is_cut_and_removed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_dns(monkeypatch, PUBLIC)

    async def endless() -> AsyncIterator[bytes]:
        for _ in range(64):  # no Content-Length: 8 MiB sent against a 1 MiB limit
            yield b"x" * (128 * 1024)

    use_transport(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=endless()))
    err = error_of(ingest.fetch_audio("https://example.com/big.mp3", str(tmp_path)))
    assert (err.code, err.params) == ("TOO_LARGE", {"max_mb": 1})
    assert list(tmp_path.iterdir()) == []  # no partial file left behind


def test_total_download_time_is_limited(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_dns(monkeypatch, PUBLIC)
    monkeypatch.setattr(ingest, "DOWNLOAD_TIMEOUT_S", 0.3)

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return audio_response()

    use_transport(monkeypatch, slow)
    started = time.monotonic()
    assert error_of(ingest.fetch_audio("https://example.com/a.mp3", str(tmp_path))).code == "TIMEOUT"
    assert time.monotonic() - started < 3


def test_not_media_after_download_is_removed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_dns(monkeypatch, PUBLIC)
    use_transport(monkeypatch, lambda request: audio_response(b"not really audio"))

    async def reject(path: str) -> float:
        raise AppError("UNSUPPORTED_FILE", "nope")

    monkeypatch.setattr(ingest, "probe_file", reject)
    assert error_of(ingest.fetch_audio("https://example.com/a.mp3", str(tmp_path))).code == "UNSUPPORTED_FILE"
    assert list(tmp_path.iterdir()) == []


def test_too_long_after_download_is_removed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_dns(monkeypatch, PUBLIC)
    use_transport(monkeypatch, lambda request: audio_response())
    patch_probe_file(monkeypatch, 121 * 60)
    err = error_of(ingest.fetch_audio("https://example.com/a.mp3", str(tmp_path)))
    assert (err.code, err.params) == ("TOO_LONG", {"max_min": 120})
    assert list(tmp_path.iterdir()) == []


def test_unexpected_errors_become_download_failed_without_details(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_dns(monkeypatch, PUBLIC)

    def boom(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("secret detail at /home/someone/private")

    use_transport(monkeypatch, boom)
    err = error_of(ingest.fetch_audio("https://example.com/a.mp3", str(tmp_path)))
    assert err.code == "DOWNLOAD_FAILED"
    assert "secret" not in err.message and "/home" not in err.message
    assert err.__cause__ is None and err.__suppress_context__


def test_invalid_and_blocked_urls_never_reach_the_network(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[httpx.Request] = []
    use_transport(monkeypatch, lambda request: (calls.append(request), audio_response())[1])
    fake_dns(monkeypatch, PUBLIC)
    for url, code in [
        ("http://127.0.0.1/a.mp3", "URL_BLOCKED"),
        ("http://localhost/a.mp3", "URL_BLOCKED"),
        ("ftp://example.com/a.mp3", "URL_INVALID"),
        ("file:///etc/passwd", "URL_INVALID"),
        ("http://user:pw@example.com/a.mp3", "URL_BLOCKED"),
    ]:
        assert error_of(ingest.fetch_audio(url, str(tmp_path))).code == code
        assert error_of(ingest.probe_url(url)).code == code
    assert calls == []


def test_probe_url_direct_checks_headers_only(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_dns(monkeypatch, PUBLIC)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return audio_response(b"x" * 4096)

    use_transport(monkeypatch, handler)
    probe = run(ingest.probe_url("https://example.com/podcasts/Episode%2012.mp3?x=1"))
    assert (probe.title, probe.duration_s, probe.kind) == ("Episode 12", None, "direct")
    assert len(seen) == 1 and seen[0].url.host == "93.184.216.34"


def test_probe_url_direct_rejects_bad_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_dns(monkeypatch, PUBLIC)
    use_transport(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>"))
    assert error_of(ingest.probe_url("https://example.com/page")).code == "UNSUPPORTED_FILE"
    use_transport(monkeypatch, lambda request: audio_response(b"x" * (2 * 1024 * 1024)))
    assert error_of(ingest.probe_url("https://example.com/big.mp3")).code == "TOO_LARGE"
    use_transport(monkeypatch, lambda request: httpx.Response(302, headers={"location": "http://10.1.2.3/x"}))
    assert error_of(ingest.probe_url("https://example.com/a.mp3")).code == "URL_BLOCKED"


def test_url_lookalike_of_youtube_is_downloaded_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_dns(monkeypatch, {"evilyoutube.com": ["93.184.216.34"]})
    use_transport(monkeypatch, lambda request: audio_response())
    probe = run(ingest.probe_url("https://evilyoutube.com/watch?v=abc"))
    assert probe.kind == "direct"


def test_logs_never_contain_urls_or_names(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    fake_dns(monkeypatch, PUBLIC)
    use_transport(monkeypatch, lambda request: audio_response())
    patch_probe_file(monkeypatch)
    run(ingest.probe_url("https://example.com/super-secret-name.mp3?token=abc123"))
    run(ingest.fetch_audio("https://example.com/super-secret-name.mp3?token=abc123", str(tmp_path)))
    text = caplog.text
    assert "super-secret-name" not in text and "abc123" not in text and "93.184.216.34" not in text


# ======================================================= yt-dlp (fake executable)

FAKE_YTDLP = r'''#!__PYTHON__
import json, os, sys, time
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
with open(os.environ["FAKE_PID"], "w") as pid:
    pid.write(str(os.getpid()))
mode = os.environ.get("FAKE_MODE", "ok")
if mode == "sleep":
    time.sleep(60)
if mode.startswith("err:"):
    sys.stderr.write(mode[4:] + "\n")
    sys.exit(1)
if "--dump-single-json" in args:
    sys.stdout.write(os.environ.get("FAKE_JSON", "{}"))
    sys.exit(0)
outdir = os.path.dirname(args[args.index("-o") + 1]).replace("%%", "%")
if mode == "nofile_large":
    sys.stdout.write("[download] File is larger than max-filesize (999 bytes > 1 bytes). Aborting.\n")
    sys.exit(0)
if mode == "nofile":
    sys.exit(0)
with open(os.path.join(outdir, "vid123.m4a"), "wb") as media:
    media.write(b"\0" * 2048)
with open(os.path.join(outdir, "vid123.m4a.part"), "wb") as junk:
    junk.write(b"\0" * 4096)
with open(os.path.join(outdir, "vid123.info.json"), "w") as meta:
    json.dump({"id": "vid123", "title": "Fake  Title\n", "duration": 42}, meta)
sys.exit(0)
'''


@pytest.fixture
def fake_ytdlp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "yt-dlp"
    script.write_text(FAKE_YTDLP.replace("__PYTHON__", sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    log_file = tmp_path / "ytdlp.log"
    pid_file = tmp_path / "ytdlp.pid"
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_LOG", str(log_file))
    monkeypatch.setenv("FAKE_PID", str(pid_file))

    def configure(mode: str = "ok", json_text: str = "{}") -> None:
        monkeypatch.setenv("FAKE_MODE", mode)
        monkeypatch.setenv("FAKE_JSON", json_text)

    def calls() -> list[list[str]]:
        if not log_file.exists():
            return []
        return [json.loads(line) for line in log_file.read_text().splitlines()]

    return SimpleNamespace(configure=configure, calls=calls, pid_file=pid_file)


VIDEO_URL = "https://www.youtube.com/watch?v=abc123"

ERROR_CASES = [
    ("ERROR: [youtube] abc: Sign in to confirm you\u2019re not a bot. Use --cookies-from-browser or --cookies", "YOUTUBE_BLOCKED"),
    ("ERROR: [youtube] abc: Sign in to confirm you're not a bot", "YOUTUBE_BLOCKED"),
    ("ERROR: unable to download video data: HTTP Error 403: Forbidden", "YOUTUBE_BLOCKED"),
    ("ERROR: unable to download video data: HTTP Error 429: Too Many Requests", "YOUTUBE_BLOCKED"),
    ("ERROR: [youtube] abc: LOGIN_REQUIRED", "YOUTUBE_BLOCKED"),
    ("ERROR: [youtube] abc: Video unavailable", "UNAVAILABLE"),
    ("ERROR: [youtube] abc: Private video. Sign in if you've been granted access to this video", "UNAVAILABLE"),
    ("ERROR: [youtube] abc: Join this channel to get access to members-only content", "UNAVAILABLE"),
    ("ERROR: [youtube] abc: Sign in to confirm your age. This video may be inappropriate for some users.", "UNAVAILABLE"),
    ("ERROR: Unsupported URL: https://www.youtube.com/about", "URL_INVALID"),
    ("ERROR: something odd happened in /home/secret/dir SECRETTOKEN", "DOWNLOAD_FAILED"),
]


@needs_posix
def test_probe_ytdlp_ok(fake_ytdlp: SimpleNamespace) -> None:
    info = {"_type": "video", "title": "  Hello\n World ", "duration": 125.5, "is_live": False, "live_status": "not_live"}
    fake_ytdlp.configure(json_text=json.dumps(info))
    probe = run(ingest.probe_url(VIDEO_URL))
    assert (probe.title, probe.duration_s, probe.kind) == ("Hello World", 125.5, "ytdlp")
    args = fake_ytdlp.calls()[0]
    for flag in ("--dump-single-json", "--no-playlist", "--skip-download", "--no-warnings"):
        assert flag in args
    assert "--proxy" not in args and "--cookies" not in args
    assert args[-2:] == ["--", VIDEO_URL]


@needs_posix
def test_ytdlp_uses_proxy_and_cookies_when_configured(monkeypatch: pytest.MonkeyPatch, fake_ytdlp: SimpleNamespace) -> None:
    monkeypatch.setattr(ingest, "settings", make_settings(ytdlp_proxy="http://proxy.internal:3128", ytdlp_cookies_file="/etc/cookies.txt"))
    fake_ytdlp.configure(json_text=json.dumps({"title": "T", "duration": 10}))
    run(ingest.probe_url("https://youtu.be/abc123"))
    args = fake_ytdlp.calls()[0]
    assert args[args.index("--proxy") + 1] == "http://proxy.internal:3128"
    assert args[args.index("--cookies") + 1] == "/etc/cookies.txt"


@needs_posix
@pytest.mark.parametrize(
    "info",
    [{"is_live": True}, {"live_status": "is_live"}, {"live_status": "is_upcoming"}, {"live_status": "post_live"}, {"availability": "private"}],
)
def test_probe_ytdlp_rejects_live_scheduled_and_private(fake_ytdlp: SimpleNamespace, info: dict) -> None:
    fake_ytdlp.configure(json_text=json.dumps({"title": "T", "duration": 60, **info}))
    assert error_of(ingest.probe_url(VIDEO_URL)).code == "UNAVAILABLE"


@needs_posix
def test_probe_ytdlp_accepts_finished_live_stream(fake_ytdlp: SimpleNamespace) -> None:
    fake_ytdlp.configure(json_text=json.dumps({"title": "T", "duration": 60, "live_status": "was_live"}))
    assert run(ingest.probe_url(VIDEO_URL)).duration_s == 60


@needs_posix
def test_probe_ytdlp_too_long(fake_ytdlp: SimpleNamespace) -> None:
    fake_ytdlp.configure(json_text=json.dumps({"title": "T", "duration": 120 * 60 + 1}))
    err = error_of(ingest.probe_url(VIDEO_URL))
    assert (err.code, err.params) == ("TOO_LONG", {"max_min": 120})
    fake_ytdlp.configure(json_text=json.dumps({"title": "T", "duration": 120 * 60}))
    assert run(ingest.probe_url(VIDEO_URL)).duration_s == 7200


@needs_posix
def test_probe_ytdlp_unknown_duration_is_none(fake_ytdlp: SimpleNamespace) -> None:
    fake_ytdlp.configure(json_text=json.dumps({"title": "T"}))
    assert run(ingest.probe_url(VIDEO_URL)).duration_s is None


@needs_posix
def test_probe_ytdlp_rejects_playlist_json(fake_ytdlp: SimpleNamespace) -> None:
    fake_ytdlp.configure(json_text=json.dumps({"_type": "playlist", "title": "P", "entries": [{"id": "a"}]}))
    assert error_of(ingest.probe_url(VIDEO_URL)).code == "URL_INVALID"


@needs_posix
def test_probe_ytdlp_unreadable_json(fake_ytdlp: SimpleNamespace) -> None:
    fake_ytdlp.configure(json_text="not json at all")
    assert error_of(ingest.probe_url(VIDEO_URL)).code == "DOWNLOAD_FAILED"


@needs_posix
@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/playlist?list=PL123",
        "https://www.youtube.com/@somechannel",
        "https://www.youtube.com/@somechannel/videos",
        "https://www.youtube.com/channel/UC123",
        "https://www.youtube.com/c/SomeName",
        "https://www.youtube.com/user/someone",
        "https://www.youtube.com/results?search_query=x",
        "https://music.youtube.com/playlist?list=PL123",
    ],
)
def test_playlists_and_channels_are_refused_without_running_ytdlp(fake_ytdlp: SimpleNamespace, tmp_path: Path, url: str) -> None:
    assert error_of(ingest.probe_url(url)).code == "URL_INVALID"
    assert error_of(ingest.fetch_audio(url, str(tmp_path / "work"))).code == "URL_INVALID"
    assert fake_ytdlp.calls() == []


@needs_posix
def test_single_video_inside_a_playlist_link_is_allowed(fake_ytdlp: SimpleNamespace) -> None:
    fake_ytdlp.configure(json_text=json.dumps({"title": "T", "duration": 5}))
    run(ingest.probe_url("https://www.youtube.com/watch?v=abc123&list=PL123"))
    assert "--no-playlist" in fake_ytdlp.calls()[0]


@needs_posix
@pytest.mark.parametrize("stderr,code", ERROR_CASES)
def test_ytdlp_errors_are_translated_on_probe(fake_ytdlp: SimpleNamespace, caplog: pytest.LogCaptureFixture, stderr: str, code: str) -> None:
    caplog.set_level(logging.DEBUG)
    fake_ytdlp.configure(mode="err:" + stderr)
    err = error_of(ingest.probe_url(VIDEO_URL))
    assert err.code == code
    assert "SECRETTOKEN" not in err.message and "/home/" not in err.message and "ERROR" not in err.message
    assert "SECRETTOKEN" not in caplog.text and "/home/secret" not in caplog.text and "abc123" not in caplog.text


@needs_posix
@pytest.mark.parametrize("stderr,code", ERROR_CASES)
def test_ytdlp_errors_are_translated_on_fetch(fake_ytdlp: SimpleNamespace, tmp_path: Path, stderr: str, code: str) -> None:
    fake_ytdlp.configure(mode="err:" + stderr)
    work = tmp_path / "work"
    assert error_of(ingest.fetch_audio(VIDEO_URL, str(work))).code == code
    assert list(work.iterdir()) == []  # the per-download folder is always cleaned


@needs_posix
def test_fetch_ytdlp_ok(monkeypatch: pytest.MonkeyPatch, fake_ytdlp: SimpleNamespace, tmp_path: Path) -> None:
    patch_probe_file(monkeypatch, 42.0)
    fake_ytdlp.configure()
    work = tmp_path / "work"
    url = "https://youtu.be/vid123"
    info = run(ingest.fetch_audio(url, str(work)))
    assert (info.title, info.duration_s, info.source) == ("Fake Title", 42.0, url)
    assert list(work.iterdir()) == [Path(info.path)]  # leftovers (.part, .info.json, folder) are gone
    assert Path(info.path).suffix == ".m4a" and Path(info.path).stat().st_size == 2048
    assert "vid123" not in Path(info.path).name
    args = fake_ytdlp.calls()[0]
    assert args[args.index("-f") + 1] == "bestaudio/best"
    assert "--no-playlist" in args and "--no-warnings" in args
    assert args[args.index("--max-filesize") + 1] == "1M"
    assert args[args.index("--socket-timeout") + 1] == "20"
    assert args[args.index("--retries") + 1] == "2"
    template = args[args.index("-o") + 1]
    assert template.startswith(str(work)) and template.endswith("%(id)s.%(ext)s")
    assert args[-2:] == ["--", url]


@needs_posix
def test_fetch_ytdlp_too_long_is_removed(monkeypatch: pytest.MonkeyPatch, fake_ytdlp: SimpleNamespace, tmp_path: Path) -> None:
    patch_probe_file(monkeypatch, 121 * 60)
    fake_ytdlp.configure()
    work = tmp_path / "work"
    err = error_of(ingest.fetch_audio(VIDEO_URL, str(work)))
    assert (err.code, err.params) == ("TOO_LONG", {"max_min": 120})
    assert list(work.iterdir()) == []


@needs_posix
def test_fetch_ytdlp_without_output(fake_ytdlp: SimpleNamespace, tmp_path: Path) -> None:
    fake_ytdlp.configure(mode="nofile_large")
    err = error_of(ingest.fetch_audio(VIDEO_URL, str(tmp_path / "w1")))
    assert (err.code, err.params) == ("TOO_LARGE", {"max_mb": 1})
    fake_ytdlp.configure(mode="nofile")
    assert error_of(ingest.fetch_audio(VIDEO_URL, str(tmp_path / "w2"))).code == "DOWNLOAD_FAILED"


def wait_until_gone(pid: int) -> bool:
    for _ in range(60):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


@needs_posix
def test_ytdlp_probe_timeout_kills_the_process(monkeypatch: pytest.MonkeyPatch, fake_ytdlp: SimpleNamespace) -> None:
    monkeypatch.setattr(ingest, "YTDLP_PROBE_TIMEOUT_S", 1.5)
    fake_ytdlp.configure(mode="sleep")
    started = time.monotonic()
    assert error_of(ingest.probe_url(VIDEO_URL)).code == "TIMEOUT"
    assert time.monotonic() - started < 15
    assert wait_until_gone(int(fake_ytdlp.pid_file.read_text()))


@needs_posix
def test_ytdlp_download_timeout_kills_the_process(monkeypatch: pytest.MonkeyPatch, fake_ytdlp: SimpleNamespace, tmp_path: Path) -> None:
    monkeypatch.setattr(ingest, "YTDLP_DOWNLOAD_TIMEOUT_S", 1.5)
    fake_ytdlp.configure(mode="sleep")
    work = tmp_path / "work"
    assert error_of(ingest.fetch_audio(VIDEO_URL, str(work))).code == "TIMEOUT"
    assert wait_until_gone(int(fake_ytdlp.pid_file.read_text()))
    assert list(work.iterdir()) == []


@needs_posix
def test_cancellation_kills_the_child(monkeypatch: pytest.MonkeyPatch, fake_ytdlp: SimpleNamespace) -> None:
    fake_ytdlp.configure(mode="sleep")

    async def scenario() -> None:
        task = asyncio.create_task(ingest.probe_url(VIDEO_URL))
        await asyncio.sleep(1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    assert wait_until_gone(int(fake_ytdlp.pid_file.read_text()))


def test_missing_binaries_are_internal_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))  # an empty directory
    err = error_of(ingest.probe_url(VIDEO_URL))
    assert (err.code, err.http_status) == ("INTERNAL", 500) and "yt-dlp" in err.message
    err = error_of(ingest.probe_file(str(tmp_path / "x.mp3")))
    assert err.code == "INTERNAL" and "ffprobe" in err.message
    work = tmp_path / "work"
    info = AudioInfo(path=str(tmp_path / "x.mp3"), duration_s=5, title="t", source="s")
    err = error_of(ingest.make_chunks(info, str(work)))
    assert err.code == "INTERNAL" and "ffmpeg" in err.message
    assert list(work.iterdir()) == []


# ======================================================= uploads and ffmpeg (real tools)


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args], check=True, capture_output=True)


def has_encoder(name: str) -> bool:
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return name in out


def make_tone(path: Path, seconds: float, codec: str = "pcm_s16le") -> Path:
    ffmpeg("-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:a", codec, str(path))
    return path


@pytest.fixture(scope="module")
def tone25(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg/ffprobe are not installed")
    return make_tone(tmp_path_factory.mktemp("media") / "tone25.wav", 25)


@needs_ffmpeg
def test_probe_file_reports_the_duration(tone25: Path) -> None:
    assert run(ingest.probe_file(str(tone25))) == pytest.approx(25.0, abs=0.1)


@needs_ffmpeg
@pytest.mark.parametrize(
    "ext,codec,video",
    [("mp3", "libmp3lame", False), ("m4a", "aac", False), ("flac", "flac", False), ("ogg", "libvorbis", False),
     ("webm", "libopus", False), ("wav", "pcm_s16le", False), ("mp4", "aac", True)],
)
def test_common_containers_pass_the_format_whitelist(tmp_path: Path, ext: str, codec: str, video: bool) -> None:
    if not has_encoder(codec):
        pytest.skip(f"ffmpeg build has no {codec}")
    target = tmp_path / f"sample.{ext}"
    if video:
        ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=3",
               "-shortest", "-c:a", codec, "-c:v", "mpeg4", str(target))
    else:
        make_tone(target, 3, codec)
    assert run(ingest.probe_file(str(target))) == pytest.approx(3.0, abs=0.2)
    info = AudioInfo(path=str(target), duration_s=3.0, title="t", source="s")
    chunks = run(ingest.make_chunks(info, str(tmp_path / "work")))
    assert len(chunks) == 1 and chunks[0].duration_s == pytest.approx(3.0, abs=0.3)


@needs_ffmpeg
def test_probe_file_rejects_files_without_audio(tmp_path: Path, tone25: Path) -> None:
    junk = tmp_path / "junk.mp3"
    junk.write_bytes(os.urandom(4096))
    text = tmp_path / "notes.mp3"
    text.write_text("hello world\n" * 50)
    video = tmp_path / "video_only.mp4"
    ffmpeg("-f", "lavfi", "-i", "color=c=black:s=64x64:d=1", "-an", "-c:v", "mpeg4", str(video))
    for target in (junk, text, video, tmp_path / "missing.mp3", tmp_path):
        err = error_of(ingest.probe_file(str(target)))
        assert err.code == "UNSUPPORTED_FILE"
        assert str(tmp_path) not in err.message


@needs_ffmpeg
def test_probe_file_falls_back_to_decoding_when_duration_is_missing(monkeypatch: pytest.MonkeyPatch, tone25: Path) -> None:
    async def no_duration(path: str) -> dict:
        return {"streams": [{"codec_type": "audio"}], "format": {"duration": "N/A"}}

    monkeypatch.setattr(ingest, "_ffprobe_json", no_duration)
    assert run(ingest.probe_file(str(tone25))) == pytest.approx(25.0, abs=0.1)
    assert run(ingest._decoded_duration(str(tone25))) == pytest.approx(25.0, abs=0.1)


@needs_ffmpeg
def test_playlists_and_concat_scripts_disguised_as_media_are_rejected(tmp_path: Path, tone25: Path) -> None:
    segment = tmp_path / "seg0.wav"
    shutil.copy(tone25, segment)
    hls = tmp_path / "evil.m3u8"
    hls.write_text(f"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:30\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:25.0,\nfile://{segment}\n#EXT-X-ENDLIST\n")
    concat = tmp_path / "evil.ffconcat"
    concat.write_text("ffconcat version 1.0\nfile 'seg0.wav'\n")
    for target in (hls, concat):
        assert error_of(ingest.probe_file(str(target))).code == "UNSUPPORTED_FILE"
        info = AudioInfo(path=str(target), duration_s=25.0, title="t", source="s")
        assert error_of(ingest.make_chunks(info, str(tmp_path / "work"))).code in {"UNSUPPORTED_FILE"}


@needs_ffmpeg
def test_save_upload_moves_file_under_a_safe_name(tone25: Path, tmp_path: Path) -> None:
    tmp_file = tmp_path / "spool" / "tmpabc123"
    tmp_file.parent.mkdir()
    shutil.copy(tone25, tmp_file)
    work = tmp_path / "work"
    info = run(ingest.save_upload(str(tmp_file), r"C:\fakepath\..\My Voice  Memo.WAV", str(work)))
    assert not tmp_file.exists()
    assert (info.title, info.source) == ("My Voice Memo", "My Voice Memo.WAV")
    assert info.duration_s == pytest.approx(25.0, abs=0.1)
    path = Path(info.path)
    assert path.parent == work and path.is_file() and path.suffix == ".wav"
    assert "Voice" not in path.name and ".." not in path.name


@needs_ffmpeg
def test_save_upload_sanitizes_hostile_names(tone25: Path, tmp_path: Path) -> None:
    tmp_file = tmp_path / "t1"
    shutil.copy(tone25, tmp_file)
    work = tmp_path / "work"
    info = run(ingest.save_upload(str(tmp_file), "../../etc/pass wd\x00\n.m4a;rm -rf", str(work)))
    path = Path(info.path)
    assert path.parent == work and path.suffix == ".bin"  # unsafe extension is dropped
    assert "\x00" not in info.title and "\n" not in info.title and "/" not in info.title
    assert [p.name for p in work.iterdir()] == [path.name]


@needs_ffmpeg
def test_save_upload_rejects_non_audio_and_cleans_up(tmp_path: Path) -> None:
    tmp_file = tmp_path / "t2"
    tmp_file.write_bytes(os.urandom(2048))
    work = tmp_path / "work"
    assert error_of(ingest.save_upload(str(tmp_file), "evil.mp3", str(work))).code == "UNSUPPORTED_FILE"
    assert list(work.iterdir()) == []


@needs_ffmpeg
def test_save_upload_too_long_and_too_large(monkeypatch: pytest.MonkeyPatch, tone25: Path, tmp_path: Path) -> None:
    tmp_file = tmp_path / "t3"
    shutil.copy(tone25, tmp_file)
    monkeypatch.setattr(ingest, "settings", make_settings(max_file_min=0.2, max_upload_mb=10))  # 12 s limit
    work = tmp_path / "work"
    err = error_of(ingest.save_upload(str(tmp_file), "long.wav", str(work)))
    assert (err.code, err.params) == ("TOO_LONG", {"max_min": 0.2})
    assert list(work.iterdir()) == []

    big = tmp_path / "t4"
    big.write_bytes(b"\x00" * (2 * 1024 * 1024))
    monkeypatch.setattr(ingest, "settings", make_settings(max_upload_mb=1))
    err = error_of(ingest.save_upload(str(big), "big.wav", str(work)))
    assert (err.code, err.params) == ("TOO_LARGE", {"max_mb": 1})
    assert big.exists()  # rejected before moving: the caller still owns its temp file


def test_save_upload_unexpected_error_leaks_no_path(tmp_path: Path) -> None:
    err = error_of(ingest.save_upload(str(tmp_path / "nonexistent-dir" / "x"), "a.mp3", str(tmp_path / "work")))
    assert err.code == "DOWNLOAD_FAILED"
    assert "nonexistent" not in err.message and str(tmp_path) not in err.message


def probe_stream(path: str) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,sample_rate,channels", "-of", "json", path],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)["streams"][0]


@needs_ffmpeg
def test_make_chunks_splits_without_overlap(tone25: Path, tmp_path: Path) -> None:
    info = AudioInfo(path=str(tone25), duration_s=25.0, title="t", source="s")
    work = tmp_path / "work"
    chunks = run(ingest.make_chunks(info, str(work)))
    assert len(chunks) == 3
    offsets = [c.offset_s for c in chunks]
    assert offsets[0] == 0.0 and offsets == sorted(offsets) and len(set(offsets)) == 3
    assert offsets[1] == pytest.approx(10.0, abs=0.1) and offsets[2] == pytest.approx(20.0, abs=0.1)
    assert [c.duration_s for c in chunks][:2] == [pytest.approx(10.0, abs=0.2)] * 2
    assert sum(c.duration_s for c in chunks) == pytest.approx(25.0, abs=0.5)
    assert all(Path(c.path).is_file() and Path(c.path).parent.parent == work for c in chunks)
    stream = probe_stream(chunks[0].path)
    assert (stream["codec_name"], stream["sample_rate"], stream["channels"]) == ("mp3", "16000", 1)
    assert [p.name for p in Path(chunks[0].path).parent.iterdir() if p.suffix != ".mp3"] == []  # no helper files left
    # every chunk starts where the previous one ends (within one MP3 frame plus encoder padding)
    for prev, nxt in zip(chunks, chunks[1:]):
        assert nxt.offset_s - (prev.offset_s + prev.duration_s) == pytest.approx(0.0, abs=0.15)


@needs_ffmpeg
def test_make_chunks_uses_the_configured_length(monkeypatch: pytest.MonkeyPatch, tone25: Path, tmp_path: Path) -> None:
    info = AudioInfo(path=str(tone25), duration_s=25.0, title="t", source="s")
    monkeypatch.setattr(ingest, "settings", make_settings(chunk_seconds=600))
    assert len(run(ingest.make_chunks(info, str(tmp_path / "a")))) == 1
    monkeypatch.setattr(ingest, "settings", make_settings(chunk_seconds=5))
    chunks = run(ingest.make_chunks(info, str(tmp_path / "b")))
    assert len(chunks) == 5 and chunks[-1].offset_s == pytest.approx(20.0, abs=0.1)


@needs_ffmpeg
def test_make_chunks_drops_tiny_trailing_piece(tmp_path: Path) -> None:
    source = make_tone(tmp_path / "tone.wav", 10.1)
    info = AudioInfo(path=str(source), duration_s=10.1, title="t", source="s")
    chunks = run(ingest.make_chunks(info, str(tmp_path / "work")))
    assert len(chunks) == 1 and chunks[0].duration_s == pytest.approx(10.0, abs=0.2)
    assert len(list(Path(chunks[0].path).parent.glob("chunk_*.mp3"))) == 1  # the tiny file was deleted


@needs_ffmpeg
def test_make_chunks_fails_cleanly_when_nothing_usable(tmp_path: Path) -> None:
    source = make_tone(tmp_path / "blip.wav", 0.2)
    info = AudioInfo(path=str(source), duration_s=0.2, title="t", source="s")
    work = tmp_path / "work"
    assert error_of(ingest.make_chunks(info, str(work))).code == "UNSUPPORTED_FILE"
    assert list(work.iterdir()) == []


@needs_ffmpeg
def test_make_chunks_ignores_video_tracks(tmp_path: Path) -> None:
    source = tmp_path / "clip.mp4"
    ffmpeg("-f", "lavfi", "-i", "color=c=blue:s=64x64:d=4", "-f", "lavfi", "-i", "sine=frequency=330:duration=4",
           "-shortest", "-c:v", "mpeg4", "-c:a", "aac", str(source))
    info = AudioInfo(path=str(source), duration_s=4.0, title="t", source="s")
    chunks = run(ingest.make_chunks(info, str(tmp_path / "work")))
    assert len(chunks) == 1 and probe_stream(chunks[0].path)["codec_name"] == "mp3"


@needs_ffmpeg
def test_make_chunks_runs_two_items_in_the_same_workdir(tone25: Path, tmp_path: Path) -> None:
    info = AudioInfo(path=str(tone25), duration_s=25.0, title="t", source="s")
    work = str(tmp_path / "work")
    first = run(ingest.make_chunks(info, work))
    second = run(ingest.make_chunks(info, work))
    assert {c.path for c in first}.isdisjoint({c.path for c in second})
    assert all(Path(c.path).is_file() for c in first + second)


@needs_posix
def test_workdir_with_percent_sign_works_for_ytdlp(monkeypatch: pytest.MonkeyPatch, fake_ytdlp: SimpleNamespace, tmp_path: Path) -> None:
    patch_probe_file(monkeypatch, 42.0)
    fake_ytdlp.configure()
    work = tmp_path / "we%ird dir"
    info = run(ingest.fetch_audio(VIDEO_URL, str(work)))
    assert Path(info.path).parent == work and Path(info.path).stat().st_size == 2048
    template = fake_ytdlp.calls()[0][fake_ytdlp.calls()[0].index("-o") + 1]
    assert "we%%ird dir" in template


@needs_ffmpeg
def test_workdir_with_percent_sign_works_for_chunks(tone25: Path, tmp_path: Path) -> None:
    info = AudioInfo(path=str(tone25), duration_s=25.0, title="t", source="s")
    chunks = run(ingest.make_chunks(info, str(tmp_path / "100%done")))
    assert len(chunks) == 3 and all(Path(c.path).is_file() for c in chunks)
