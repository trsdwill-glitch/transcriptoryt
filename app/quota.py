from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import math
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TypeVar

from starlette.requests import Request

from app.config import settings
from app.models import AppError

_WINDOW_S = 24 * 60 * 60
_RETENTION_S = 25 * 60 * 60
_EPSILON = 1e-7
_database_lock = threading.RLock()
_development_secret = secrets.token_bytes(32)
_T = TypeVar("_T")


@dataclass(frozen=True)
class Identity:
    cookie_hash: str
    fingerprint_hash: str
    ip_hash: str
    cookie_value: str
    is_new: bool

    @property
    def key_hashes(self) -> tuple[str, str, str]:
        return self.cookie_hash, self.fingerprint_hash, self.ip_hash


def _hash(kind: str, value: str) -> str:
    return hashlib.sha256(f"{settings.hash_salt}:{kind}:{value}".encode()).hexdigest()


def _signature(token: str) -> str:
    key = settings.secret_key.encode() if settings.secret_key else _development_secret
    return hmac.new(key, token.encode(), hashlib.sha256).hexdigest()


def _normalize_ip(value: str) -> str:
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        return "unknown"
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


def identify(request: Request) -> Identity:
    raw_cookie = request.cookies.get("tid", "")
    token, separator, signature = raw_cookie.partition(".")
    valid = bool(
        separator
        and re.fullmatch(r"[A-Za-z0-9_-]{22,128}", token)
        and re.fullmatch(r"[a-f0-9]{64}", signature)
        and hmac.compare_digest(signature, _signature(token))
    )
    if not valid:
        token = secrets.token_urlsafe(32)
    cookie_value = f"{token}.{_signature(token)}"
    fingerprint = request.headers.get("X-Client-Fp", "").strip().lower()
    if re.fullmatch(r"[a-f0-9]{64}", fingerprint) is None:
        fingerprint = token
    peer = request.client.host if request.client else "unknown"
    ip_value = peer
    if settings.trusted_proxy:
        forwarded = request.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
        if forwarded and _normalize_ip(forwarded) != "unknown":
            ip_value = forwarded
    return Identity(
        cookie_hash=_hash("cookie", token),
        fingerprint_hash=_hash("fingerprint", fingerprint),
        ip_hash=_hash("ip", _normalize_ip(ip_value)),
        cookie_value=cookie_value,
        is_new=not valid,
    )


def _with_database(operation: Callable[[sqlite3.Connection], _T]) -> _T:
    # Serialize transactions across worker threads and connections.
    with _database_lock:
        directory = Path(settings.data_dir)
        directory.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(directory / "quota.sqlite3", timeout=30, isolation_level=None)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                "CREATE TABLE IF NOT EXISTS usage ("
                "key_hash TEXT NOT NULL, ts REAL NOT NULL, seconds REAL NOT NULL);"
                "CREATE INDEX IF NOT EXISTS usage_key_ts ON usage(key_hash, ts);"
                "CREATE TABLE IF NOT EXISTS budget (day TEXT PRIMARY KEY, usd REAL NOT NULL);"
                "CREATE TABLE IF NOT EXISTS reservations ("
                "id INTEGER PRIMARY KEY, job_id TEXT NOT NULL, cookie_hash TEXT NOT NULL, "
                "fingerprint_hash TEXT NOT NULL, ip_hash TEXT NOT NULL, ts REAL NOT NULL, "
                "seconds REAL NOT NULL, refunded REAL NOT NULL DEFAULT 0);"
                "CREATE INDEX IF NOT EXISTS reservations_job ON reservations(job_id, cookie_hash);"
            )
            return operation(connection)
        finally:
            connection.close()


def _balances(connection: sqlite3.Connection, identity: Identity, now: float) -> tuple[float, int]:
    keys = identity.key_hashes
    rows = connection.execute(
        "SELECT key_hash, ts, SUM(seconds) FROM usage "
        "WHERE key_hash IN (?, ?, ?) AND ts > ? GROUP BY key_hash, ts",
        (*keys, now - _WINDOW_S),
    ).fetchall()
    usage = {key: 0.0 for key in keys}
    for key_hash, _timestamp, seconds in rows:
        usage[key_hash] += float(seconds)
    limit = float(settings.daily_quota_min) * 60
    limits = (limit, limit, limit * settings.ip_quota_multiplier)
    available = [maximum - max(0.0, usage[key]) for key, maximum in zip(keys, limits)]
    remaining = max(0.0, min(limit, *available))
    minimum = min(available)
    binding = {key for key, value in zip(keys, available) if abs(value - minimum) < _EPSILON}
    expirations = [
        timestamp + _WINDOW_S
        for key_hash, timestamp, seconds in rows
        if key_hash in binding and seconds > _EPSILON
    ]
    resets = max(0, math.ceil(min(expirations) - now)) if expirations else 0
    return remaining, resets


def _amount(value: float) -> float:
    amount = float(value)
    if not math.isfinite(amount) or amount < 0:
        raise ValueError("Reservation amounts must be finite and non-negative.")
    return amount


async def remaining_s(identity: Identity) -> float:
    def read(connection: sqlite3.Connection) -> float:
        return _balances(connection, identity, time.time())[0]

    return await asyncio.to_thread(_with_database, read)


async def quota_view(identity: Identity) -> dict:
    def read(connection: sqlite3.Connection) -> dict:
        remaining, resets = _balances(connection, identity, time.time())
        limit_min = float(settings.daily_quota_min)
        return {
            "limit_min": limit_min,
            "used_min": max(0.0, limit_min - remaining / 60),
            "remaining_min": remaining / 60,
            "resets_in_s": resets,
        }

    return await asyncio.to_thread(_with_database, read)


async def reserve(identity: Identity, seconds: float, job_id: str) -> None:
    amount = _amount(seconds)
    if amount == 0:
        return

    def write(connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            now = time.time()
            remaining, resets = _balances(connection, identity, now)
            if amount - remaining > _EPSILON:
                raise AppError(
                    "QUOTA_EXCEEDED",
                    "Your daily audio quota is exhausted.",
                    {"remaining_min": remaining / 60, "resets_in_s": resets},
                    http_status=429,
                )
            connection.executemany(
                "INSERT INTO usage(key_hash, ts, seconds) VALUES (?, ?, ?)",
                [(key, now, amount) for key in identity.key_hashes],
            )
            connection.execute(
                "INSERT INTO reservations(job_id, cookie_hash, fingerprint_hash, ip_hash, ts, seconds) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (job_id, *identity.key_hashes, now, amount),
            )
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    await asyncio.to_thread(_with_database, write)


async def refund(identity: Identity, seconds: float, job_id: str) -> None:
    amount = _amount(seconds)
    if amount == 0:
        return

    def write(connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            rows = connection.execute(
                "SELECT id, cookie_hash, fingerprint_hash, ip_hash, ts, seconds, refunded "
                "FROM reservations WHERE job_id = ? AND cookie_hash = ? AND seconds > refunded "
                "ORDER BY id",
                (job_id, identity.cookie_hash),
            ).fetchall()
            outstanding = amount
            for row_id, cookie_hash, fingerprint_hash, ip_hash, timestamp, reserved, refunded in rows:
                returned = min(outstanding, float(reserved) - float(refunded))
                if returned <= 0:
                    break
                # Preserve expiration: an old refund must not offset newer usage.
                connection.executemany(
                    "INSERT INTO usage(key_hash, ts, seconds) VALUES (?, ?, ?)",
                    [(key, timestamp, -returned) for key in (cookie_hash, fingerprint_hash, ip_hash)],
                )
                connection.execute(
                    "UPDATE reservations SET refunded = refunded + ? WHERE id = ?", (returned, row_id)
                )
                outstanding -= returned
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    await asyncio.to_thread(_with_database, write)


def budget_day() -> str:
    return datetime.fromtimestamp(time.time(), timezone.utc).date().isoformat()


async def reserve_budget(usd: float, *, day: str | None = None) -> None:
    amount = _amount(usd)
    target_day = date.fromisoformat(day or budget_day()).isoformat()
    if amount == 0:
        return

    def write(connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute("SELECT usd FROM budget WHERE day = ?", (target_day,)).fetchone()
            used = float(row[0]) if row else 0.0
            if used + amount - settings.daily_budget_usd > _EPSILON:
                raise AppError(
                    "BUDGET_EXCEEDED",
                    "The service's daily transcription budget is exhausted. Please try again tomorrow.",
                    http_status=429,
                )
            connection.execute(
                "INSERT INTO budget(day, usd) VALUES (?, ?) "
                "ON CONFLICT(day) DO UPDATE SET usd = excluded.usd",
                (target_day, used + amount),
            )
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    await asyncio.to_thread(_with_database, write)


async def refund_budget(usd: float, *, day: str | None = None) -> None:
    amount = _amount(usd)
    target_day = date.fromisoformat(day or budget_day()).isoformat()
    if amount == 0:
        return

    def write(connection: sqlite3.Connection) -> None:
        connection.execute(
            "UPDATE budget SET usd = MAX(0, usd - ?) WHERE day = ?", (amount, target_day)
        )

    await asyncio.to_thread(_with_database, write)


async def purge() -> None:
    def write(connection: sqlite3.Connection) -> None:
        cutoff = time.time() - _RETENTION_S
        old_day = (date.fromisoformat(budget_day()) - timedelta(days=3)).isoformat()
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("DELETE FROM usage WHERE ts < ?", (cutoff,))
            connection.execute("DELETE FROM reservations WHERE ts < ?", (cutoff,))
            connection.execute("DELETE FROM budget WHERE day < ?", (old_day,))
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    await asyncio.to_thread(_with_database, write)
