"""SSRF protection for user-supplied links (role B).

`validate_url` is the synchronous syntax gate, `assert_public_host` is the async
DNS gate and `is_ytdlp_url` decides which downloader handles a link.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Iterable
from typing import Optional
from urllib.parse import quote, urlsplit, urlunsplit

from app.config import settings
from app.models import AppError

__all__ = ["validate_url", "assert_public_host", "is_ytdlp_url"]

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

MAX_URL_LENGTH = 2048
ALLOWED_SCHEMES = frozenset({"http", "https"})
ALLOWED_PORTS = frozenset({80, 443, 8080, 8443})
DNS_TIMEOUT_S = 10.0

_DEFAULT_PORTS = {"http": 80, "https": 443}
_PATH_SAFE = "/%:@!$&'()*+,;=-._~"
_QUERY_SAFE = "/%:@!$&'()*+,;=?[]-._~"

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# Last label of a host that a resolver (inet_aton) could read as a number: decimal or 0x-hex.
_NUMERIC_LABEL_RE = re.compile(r"0[xX][0-9a-fA-F]*|[0-9]+", re.ASCII)
_OCTET = r"(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])"
_IPV4_RE = re.compile(rf"{_OCTET}(?:\.{_OCTET}){{3}}")
_LABEL_RE = re.compile(r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?")
_INTERNAL_SUFFIXES = (
    ".localhost", ".local", ".localdomain", ".internal", ".intranet", ".lan", ".home.arpa",
)

_BLOCKED_V4 = tuple(
    ipaddress.IPv4Network(n)
    for n in (
        "0.0.0.0/8",        # "this" network, includes 0.0.0.0
        "10.0.0.0/8",       # private
        "100.64.0.0/10",    # CGNAT
        "127.0.0.0/8",      # loopback
        "169.254.0.0/16",   # link-local, includes cloud metadata 169.254.169.254
        "172.16.0.0/12",    # private
        "192.0.0.0/24",     # IETF protocol assignments
        "192.0.2.0/24",     # TEST-NET-1
        "192.88.99.0/24",   # deprecated 6to4 relay
        "192.168.0.0/16",   # private
        "198.18.0.0/15",    # benchmarking
        "198.51.100.0/24",  # TEST-NET-2
        "203.0.113.0/24",   # TEST-NET-3
        "224.0.0.0/4",      # multicast
        "240.0.0.0/4",      # reserved, includes broadcast
    )
)
# IPv6 is an allow-list (global unicast 2000::/3) minus special-purpose ranges.
_V6_GLOBAL_UNICAST = ipaddress.IPv6Network("2000::/3")
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")
_BLOCKED_V6 = tuple(
    ipaddress.IPv6Network(n)
    for n in (
        "2001::/23",     # IETF protocol assignments (Teredo, benchmarking, ORCHID, ...)
        "2001:db8::/32", # documentation
        "2002::/16",     # 6to4 (embeds arbitrary IPv4)
        "3fff::/20",     # documentation
    )
)


def _invalid(message: str = "That doesn't look like a valid link.") -> AppError:
    return AppError("URL_INVALID", message)


def _blocked(message: str = "That link points to a destination that is not allowed.") -> AppError:
    return AppError("URL_BLOCKED", message)


def _is_public_ip(ip: IPAddress) -> bool:
    """True only for globally routable unicast addresses."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:  # ::ffff:a.b.c.d -> judge the embedded IPv4
            return _is_public_ip(ip.ipv4_mapped)
        if ip in _NAT64:  # 64:ff9b::a.b.c.d -> judge the embedded IPv4
            return _is_public_ip(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        if ip not in _V6_GLOBAL_UNICAST:
            return False
        if any(ip in net for net in _BLOCKED_V6):
            return False
        return ip.is_global
    if any(ip in net for net in _BLOCKED_V4):
        return False
    return ip.is_global


def _normalize_host(raw: str) -> tuple[str, Optional[IPAddress]]:
    """Syntax checks on a host. Returns (normalized host, ip if the host is an IP literal).

    Raises URL_INVALID for malformed hosts and URL_BLOCKED for numeric hosts written in
    decimal/octal/hex/short form, scoped IPv6 and obviously internal names.
    """
    host = raw.strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if ":" in host:  # IPv6 literal (also ::ffff:a.b.c.d)
        if "%" in host:
            raise _blocked()
        try:
            ip6 = ipaddress.IPv6Address(host)
        except ValueError:
            raise _invalid() from None
        return str(ip6), ip6
    if not host.isascii():  # IDNA first, so full-width digits/dots cannot hide an IP
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise _invalid() from None
    host = host.lower()
    if host.endswith("."):
        host = host[:-1]
    if not host or len(host) > 253:
        raise _invalid()
    if _NUMERIC_LABEL_RE.fullmatch(host.rsplit(".", 1)[-1]):
        # Resolvers read 2130706433, 0x7f000001, 0177.0.0.1 or 127.1 as IPv4: accept only a-b-c-d.
        if not _IPV4_RE.fullmatch(host):
            raise _blocked("Numeric hosts must be written as a standard IPv4 address.")
        ip4 = ipaddress.IPv4Address(host)
        return str(ip4), ip4
    labels = host.split(".")
    if not all(_LABEL_RE.fullmatch(label) for label in labels):
        raise _invalid()
    if len(labels) < 2 or host.endswith(_INTERNAL_SUFFIXES):
        raise _blocked()  # single-label and internal names never belong to a public file host
    return host, None


def validate_url(url: str) -> str:
    """Return the normalized URL or raise AppError URL_INVALID / URL_BLOCKED."""
    if not isinstance(url, str):
        raise _invalid()
    raw = url.strip()
    if not raw:
        raise _invalid("Paste a link to a video or audio file.")
    if len(raw) > MAX_URL_LENGTH:
        raise _invalid("The link is too long.")
    if _CONTROL_RE.search(raw) or "\\" in raw:
        raise _invalid()
    try:
        parts = urlsplit(raw)
        port = parts.port
        hostname = parts.hostname
        username, password = parts.username, parts.password
    except ValueError:
        raise _invalid() from None
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise _invalid("Only http and https links are supported.")
    if username is not None or password is not None or "@" in parts.netloc:
        raise _blocked("Links with a username or password are not allowed.")
    if not hostname:
        raise _invalid()
    host, literal = _normalize_host(hostname)
    if literal is not None and not _is_public_ip(literal):
        raise _blocked()
    if port is not None and port not in ALLOWED_PORTS:
        raise _blocked("That port is not allowed.")

    netloc = f"[{host}]" if isinstance(literal, ipaddress.IPv6Address) else host
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        netloc = f"{netloc}:{port}"
    path = quote(parts.path, safe=_PATH_SAFE) or "/"
    query = quote(parts.query, safe=_QUERY_SAFE)
    return urlunsplit((scheme, netloc, path, query, ""))


async def _resolve(name: str) -> list[IPAddress]:
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(name, None, type=socket.SOCK_STREAM), timeout=DNS_TIMEOUT_S
        )
    except TimeoutError:
        raise AppError("DOWNLOAD_FAILED", "The host took too long to resolve.") from None
    except socket.gaierror as exc:
        if exc.errno == socket.EAI_AGAIN:
            raise AppError("DOWNLOAD_FAILED", "The host could not be resolved right now.") from None
        raise _invalid("The host of that link could not be found.") from None
    except UnicodeError:
        raise _invalid() from None
    found: list[IPAddress] = []
    for info in infos:
        text = str(info[4][0]).split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(text)
        except ValueError:
            raise _blocked() from None
        if ip not in found:
            found.append(ip)
    return found


async def assert_public_host(host: str) -> list[str]:
    """Resolve `host` and return its IPs; URL_BLOCKED if ANY address is not public."""
    name, literal = _normalize_host(host if isinstance(host, str) else "")
    if literal is not None:
        if not _is_public_ip(literal):
            raise _blocked()
        return [str(literal)]
    addresses = await _resolve(name)
    if not addresses:
        raise _invalid("The host of that link could not be found.")
    if not all(_is_public_ip(ip) for ip in addresses):
        raise _blocked()
    return [str(ip) for ip in addresses]


def _ytdlp_hosts() -> frozenset[str]:
    """settings.ytdlp_hosts may be a comma-separated string or any iterable of hosts."""
    raw = getattr(settings, "ytdlp_hosts", ())
    items: Iterable[object] = raw.split(",") if isinstance(raw, str) else raw
    return frozenset(
        str(item).strip().lower().rstrip(".")
        for item in items
        if str(item).strip()
    )


def is_ytdlp_url(url: str) -> bool:
    """True if the URL's exact host is in settings.ytdlp_hosts."""
    try:
        host = urlsplit(url.strip()).hostname
    except (ValueError, AttributeError):
        return False
    if not host:
        return False
    return host.lower().rstrip(".") in _ytdlp_hosts()
