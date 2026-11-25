"""Tests for app.security (no network: DNS is faked at socket.getaddrinfo)."""
from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import security  # noqa: E402
from app.models import AppError  # noqa: E402

YTDLP_HOSTS = ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be")


@pytest.fixture(autouse=True)
def fixed_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(security, "settings", SimpleNamespace(ytdlp_hosts=YTDLP_HOSTS))


def fake_dns(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, list[str]]) -> None:
    """Make loop.getaddrinfo answer from `mapping`; unknown names raise NXDOMAIN."""

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


def code_of(func: Callable[..., Any], *args: Any) -> str:
    with pytest.raises(AppError) as exc:
        func(*args)
    return exc.value.code


def host_code(host: str) -> str:
    with pytest.raises(AppError) as exc:
        asyncio.run(security.assert_public_host(host))
    return exc.value.code


# ---------------------------------------------------------------- validate_url


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/a.mp3",
        "http://localhost/a.mp3",
        "http://10.0.0.1/a.mp3",
        "http://172.16.0.9/a.mp3",
        "http://192.168.1.10/a.mp3",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.64.0.1/a.mp3",
        "http://0.0.0.0/a.mp3",
        "http://[::1]/a.mp3",
        "http://[::ffff:127.0.0.1]/a.mp3",
        "http://[::ffff:7f00:1]/a.mp3",
        "http://[fe80::1]/a.mp3",
        "http://[fd00::1]/a.mp3",
        "http://printer.local/a.mp3",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://intranet/a.mp3",
    ],
)
def test_validate_url_blocks_internal_destinations(url: str) -> None:
    assert code_of(security.validate_url, url) == "URL_BLOCKED"


@pytest.mark.parametrize(
    "url",
    [
        "http://0x7f000001/",
        "http://2130706433/",
        "http://0177.0.0.1/",
        "http://017700000001/",
        "http://127.1/",
        "http://0x7f.0.0.1/",
        "http://127.0.1/",
        "http://1.1.1.01/",
        "http://127\u30020\u30020\u30021/",  # ideographic full stops normalize to dots
    ],
)
def test_validate_url_blocks_non_canonical_numeric_hosts(url: str) -> None:
    assert code_of(security.validate_url, url) == "URL_BLOCKED"


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "ftp://example.com/a.mp3", "gopher://example.com/", "javascript:alert(1)", "data:text/plain,hi"],
)
def test_validate_url_rejects_other_schemes(url: str) -> None:
    assert code_of(security.validate_url, url) == "URL_INVALID"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "example.com/a.mp3",
        "//example.com/a.mp3",
        "http://",
        "https:///a.mp3",
        "http://exa mple.com/",
        "http://exa%6dple.com/",
        "http://example.com:99999/",
        "http://example.com:abc/",
        "http://[::1/",
        "http://example.com/a\r\nHost: evil",
        "http://example.com\\@127.0.0.1/",
        "http://a..b/",
    ],
)
def test_validate_url_rejects_malformed(url: str) -> None:
    assert code_of(security.validate_url, url) == "URL_INVALID"


def test_validate_url_rejects_non_string_and_long_urls() -> None:
    assert code_of(security.validate_url, None) == "URL_INVALID"
    long_url = "https://example.com/" + "a" * 2100
    assert code_of(security.validate_url, long_url) == "URL_INVALID"
    assert security.validate_url("https://example.com/" + "a" * 2000).startswith("https://example.com/")


@pytest.mark.parametrize(
    "url",
    [
        "http://user:pass@example.com/a.mp3",
        "http://user@example.com/a.mp3",
        "http://:@example.com/a.mp3",
        "http://example.com@127.0.0.1/a.mp3",
        "https://youtube.com@evil.com/watch?v=x",
    ],
)
def test_validate_url_rejects_credentials(url: str) -> None:
    assert code_of(security.validate_url, url) == "URL_BLOCKED"


@pytest.mark.parametrize("port", [21, 22, 25, 3306, 6379, 8000, 9200, 0])
def test_validate_url_rejects_disallowed_ports(port: int) -> None:
    assert code_of(security.validate_url, f"http://example.com:{port}/a.mp3") == "URL_BLOCKED"


@pytest.mark.parametrize("port", [80, 443, 8080, 8443])
def test_validate_url_accepts_allowed_ports(port: int) -> None:
    assert security.validate_url(f"https://example.com:{port}/a.mp3").startswith("https://example.com")


def test_validate_url_normalizes() -> None:
    assert security.validate_url("  HTTPS://Example.COM:443/Path?q=1#frag ") == "https://example.com/Path?q=1"
    assert security.validate_url("http://example.com") == "http://example.com/"
    assert security.validate_url("http://example.com:8080/x") == "http://example.com:8080/x"
    assert security.validate_url("https://example.com/my song.mp3") == "https://example.com/my%20song.mp3"
    assert security.validate_url("https://example.com/a%20b.mp3?x=1&y=%2F") == "https://example.com/a%20b.mp3?x=1&y=%2F"
    assert security.validate_url("https://example.com./a.mp3") == "https://example.com/a.mp3"
    assert security.validate_url("https://b\u00fccher.example/x") == "https://xn--bcher-kva.example/x"


def test_validate_url_accepts_public_ip_literals() -> None:
    assert security.validate_url("http://8.8.8.8/a.mp3") == "http://8.8.8.8/a.mp3"
    assert security.validate_url("https://[2606:4700:4700::1111]/a.mp3") == "https://[2606:4700:4700::1111]/a.mp3"


# ---------------------------------------------------------- assert_public_host


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1", "10.0.0.1", "172.16.5.4", "172.31.255.255", "192.168.1.1", "169.254.169.254",
        "100.64.0.1", "100.127.255.255", "0.0.0.0", "224.0.0.1", "240.0.0.1", "255.255.255.255",
        "192.0.2.1", "198.18.0.1",
        "[::1]", "::1", "::", "::ffff:127.0.0.1", "::ffff:10.0.0.1", "::ffff:7f00:1", "::127.0.0.1",
        "fe80::1", "fc00::1", "fd12:3456::1", "ff02::1", "2001:db8::1", "2002:7f00:1::1",
        "64:ff9b::7f00:1", "64:ff9b:1::1",
        "0x7f000001", "2130706433", "0177.0.0.1", "127.1",
        "localhost", "foo.localhost", "redis", "box.internal",
    ],
)
def test_assert_public_host_blocks(host: str) -> None:
    assert host_code(host) == "URL_BLOCKED"


def test_assert_public_host_accepts_public_literals() -> None:
    assert asyncio.run(security.assert_public_host("8.8.8.8")) == ["8.8.8.8"]
    assert asyncio.run(security.assert_public_host("172.15.255.255")) == ["172.15.255.255"]
    assert asyncio.run(security.assert_public_host("2606:4700:4700::1111")) == ["2606:4700:4700::1111"]
    assert asyncio.run(security.assert_public_host("[2606:4700:4700::1111]")) == ["2606:4700:4700::1111"]
    assert asyncio.run(security.assert_public_host("64:ff9b::808:808")) == ["64:ff9b::808:808"]


def test_assert_public_host_returns_resolved_ips(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_dns(monkeypatch, {"example.com": ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946", "93.184.216.34"]})
    ips = asyncio.run(security.assert_public_host("Example.COM."))
    assert ips == ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"]


def test_assert_public_host_requires_all_addresses_public(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_dns(
        monkeypatch,
        {
            "mixed.example.com": ["93.184.216.34", "10.0.0.5"],
            "mixed6.example.com": ["93.184.216.34", "::1"],
            "mapped.example.com": ["::ffff:127.0.0.1"],
            "rebind.example.com": ["169.254.169.254"],
            "cgnat.example.com": ["100.64.1.1"],
        },
    )
    for name in ("mixed.example.com", "mixed6.example.com", "mapped.example.com", "rebind.example.com", "cgnat.example.com"):
        assert host_code(name) == "URL_BLOCKED", name


def test_assert_public_host_dns_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_dns(monkeypatch, {})
    assert host_code("does-not-exist.example.com") == "URL_INVALID"
    assert host_code("") == "URL_INVALID"

    def temporary_failure(*_: Any, **__: Any) -> list:
        raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")

    monkeypatch.setattr(socket, "getaddrinfo", temporary_failure)
    assert host_code("example.com") == "DOWNLOAD_FAILED"


def test_assert_public_host_empty_answer_is_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [])
    assert host_code("example.com") == "URL_INVALID"


def test_blocked_error_has_no_stack_details() -> None:
    with pytest.raises(AppError) as exc:
        security.validate_url("http://169.254.169.254/latest/meta-data/")
    assert "169.254" not in exc.value.message
    assert exc.value.http_status == 400


# ------------------------------------------------------------------ is_ytdlp_url


@pytest.mark.parametrize(
    "url",
    [
        "https://youtube.com/watch?v=abc",
        "https://www.youtube.com/watch?v=abc",
        "https://WWW.YouTube.com/watch?v=abc",
        "https://m.youtube.com/watch?v=abc",
        "https://music.youtube.com/watch?v=abc",
        "https://youtu.be/abc",
        "https://youtube.com./watch?v=abc",
        "http://youtube.com:8080/watch?v=abc",
    ],
)
def test_is_ytdlp_url_true(url: str) -> None:
    assert security.is_ytdlp_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://evilyoutube.com/watch?v=abc",
        "https://notyoutu.be/abc",
        "https://www.youtu.be/abc",
        "https://youtube.com.evil.com/watch?v=abc",
        "https://youtube.com@evil.com/watch?v=abc",
        "https://evil.com/?u=https://youtube.com/watch",
        "https://sub.youtube.com/watch",
        "https://example.com/a.mp3",
        "youtube.com/watch?v=abc",
        "garbage",
        "",
        "http://[::1/",
    ],
)
def test_is_ytdlp_url_false(url: str) -> None:
    assert security.is_ytdlp_url(url) is False


def test_is_ytdlp_url_accepts_string_or_iterable_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(security, "settings", SimpleNamespace(ytdlp_hosts="youtube.com, www.youtube.com ,youtu.be"))
    assert security.is_ytdlp_url("https://youtu.be/abc") is True
    assert security.is_ytdlp_url("https://evilyoutube.com/abc") is False
    monkeypatch.setattr(security, "settings", SimpleNamespace(ytdlp_hosts=["vimeo.com"]))
    assert security.is_ytdlp_url("https://vimeo.com/123") is True
    assert security.is_ytdlp_url("https://www.vimeo.com/123") is False
    assert security.is_ytdlp_url("https://youtube.com/watch?v=abc") is False
    monkeypatch.setattr(security, "settings", SimpleNamespace(ytdlp_hosts=["www.youtube.com"]))
    assert security.is_ytdlp_url("https://www.youtube.com/watch?v=abc") is True
    assert security.is_ytdlp_url("https://youtube.com/watch?v=abc") is False
    monkeypatch.setattr(security, "settings", SimpleNamespace(ytdlp_hosts=()))
    assert security.is_ytdlp_url("https://youtube.com/watch?v=abc") is False
