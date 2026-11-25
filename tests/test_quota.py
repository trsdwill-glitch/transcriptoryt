from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from starlette.requests import Request

from app import quota
from app.config import Settings
from app.models import AppError


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[float]:
    now = [1_800_000_000.0]
    monkeypatch.setattr(quota, "time", type("Clock", (), {"time": staticmethod(lambda: now[0])}))
    monkeypatch.setattr(
        quota,
        "settings",
        Settings(data_dir=str(tmp_path), secret_key="test-secret", hash_salt="test-salt"),
    )
    return now


def identity(cookie: str = "a", fingerprint: str = "a", ip: str = "a") -> quota.Identity:
    return quota.Identity(f"cookie-{cookie}", f"fingerprint-{fingerprint}", f"ip-{ip}", "signed", False)


def request(
    *, cookie: str = "", fingerprint: str = "", ip: str = "192.0.2.1", forwarded: str = ""
) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if cookie:
        headers.append((b"cookie", f"tid={cookie}".encode()))
    if fingerprint:
        headers.append((b"x-client-fp", fingerprint.encode()))
    if forwarded:
        headers.append((b"x-forwarded-for", forwarded.encode()))
    return Request({"type": "http", "headers": headers, "client": (ip, 12345)})


@pytest.mark.asyncio
async def test_reserve_and_refund_are_capped_by_job(clock: list[float]) -> None:
    user = identity()
    assert await quota.remaining_s(user) == 7200
    await quota.reserve(user, 120, "job")
    await quota.reserve(user, 60, "job")
    assert await quota.remaining_s(user) == 7020
    await quota.refund(user, 30, "job")
    assert await quota.remaining_s(user) == 7050
    await quota.refund(user, 1000, "other-job")
    assert await quota.remaining_s(user) == 7050
    await quota.refund(user, 1000, "job")
    await quota.refund(user, 1000, "job")
    assert await quota.remaining_s(user) == 7200


@pytest.mark.asyncio
async def test_remaining_is_minimum_of_three_identifiers(clock: list[float]) -> None:
    user = identity()
    await quota.reserve(identity("a", "b", "b"), 600, "cookie")
    await quota.reserve(identity("b", "a", "c"), 1200, "fingerprint")
    assert await quota.remaining_s(user) == 6000
    assert (await quota.quota_view(user))["used_min"] == 20


@pytest.mark.asyncio
async def test_ip_has_triple_quota_and_constrains_new_users(clock: list[float]) -> None:
    for index in range(3):
        await quota.reserve(identity(str(index), str(index), "shared"), 7200, f"job-{index}")
    newcomer = identity("new", "new", "shared")
    assert await quota.remaining_s(newcomer) == 0
    with pytest.raises(AppError) as error:
        await quota.reserve(newcomer, 1, "fourth")
    assert error.value.code == "QUOTA_EXCEEDED"
    assert error.value.params == {"remaining_min": 0, "resets_in_s": 86400}
    assert await quota.remaining_s(identity("new", "new", "different")) == 7200


@pytest.mark.asyncio
async def test_rolling_window_expires_at_24_hours(clock: list[float]) -> None:
    user = identity()
    await quota.reserve(user, 60, "old")
    clock[0] += 3600
    await quota.reserve(user, 120, "new")
    assert (await quota.quota_view(user))["resets_in_s"] == 23 * 3600
    clock[0] += 23 * 3600
    assert await quota.remaining_s(user) == 7080
    clock[0] += 3600
    assert await quota.quota_view(user) == {
        "limit_min": 120,
        "used_min": 0,
        "remaining_min": 120,
        "resets_in_s": 0,
    }


@pytest.mark.asyncio
async def test_old_refund_does_not_create_new_rolling_window_credit(clock: list[float]) -> None:
    user = identity()
    await quota.reserve(user, 7200, "old")
    clock[0] += 86400
    await quota.reserve(user, 7200, "new")
    await quota.refund(user, 7200, "old")
    assert await quota.remaining_s(user) == 0


@pytest.mark.asyncio
async def test_concurrent_reservations_are_atomic(clock: list[float]) -> None:
    user = identity()
    results = await asyncio.gather(
        quota.reserve(user, 5000, "one"), quota.reserve(user, 5000, "two"), return_exceptions=True
    )
    assert sum(isinstance(result, AppError) for result in results) == 1
    assert await quota.remaining_s(user) == 2200


@pytest.mark.asyncio
async def test_budget_limit_refund_and_utc_day(clock: list[float]) -> None:
    first_day = quota.budget_day()
    await quota.reserve_budget(0.4, day=first_day)
    with pytest.raises(AppError) as error:
        await quota.reserve_budget(0.11)
    assert error.value.code == "BUDGET_EXCEEDED"
    await quota.refund_budget(0.1, day=first_day)
    await quota.reserve_budget(0.2, day=first_day)
    clock[0] += 86400
    await quota.reserve_budget(0.5)
    await quota.refund_budget(0.5, day=first_day)
    with pytest.raises(AppError):
        await quota.reserve_budget(0.01)


@pytest.mark.asyncio
async def test_concurrent_budget_reservations_are_atomic(clock: list[float]) -> None:
    results = await asyncio.gather(
        quota.reserve_budget(0.3), quota.reserve_budget(0.3), return_exceptions=True
    )
    assert sum(isinstance(result, AppError) for result in results) == 1


def test_signed_cookie_fingerprint_and_tamper_detection(clock: list[float]) -> None:
    initial = quota.identify(request(fingerprint="A" * 64))
    assert initial.is_new
    same = quota.identify(request(cookie=initial.cookie_value, fingerprint="a" * 64))
    assert same.cookie_hash == initial.cookie_hash
    assert same.fingerprint_hash == initial.fingerprint_hash
    assert not same.is_new
    forged = initial.cookie_value[:-1] + ("0" if initial.cookie_value[-1] != "0" else "1")
    replacement = quota.identify(request(cookie=forged))
    assert replacement.is_new
    assert replacement.cookie_hash != initial.cookie_hash
    expected = hashlib.sha256(f"test-salt:fingerprint:{'a' * 64}".encode()).hexdigest()
    assert same.fingerprint_hash == expected


def test_missing_fingerprint_does_not_share_a_global_quota(clock: list[float]) -> None:
    first = quota.identify(request())
    second = quota.identify(request())
    assert first.fingerprint_hash != second.fingerprint_hash
    renewed = quota.identify(request(cookie=first.cookie_value))
    assert renewed.fingerprint_hash == first.fingerprint_hash


def test_ipv6_groups_by_64_and_ignores_untrusted_forwarding(clock: list[float]) -> None:
    first = quota.identify(request(ip="2001:db8:abcd:1::1"))
    same = quota.identify(request(ip="2001:db8:abcd:1:ffff::2"))
    different = quota.identify(request(ip="2001:db8:abcd:2::1"))
    assert first.ip_hash == same.ip_hash
    assert first.ip_hash != different.ip_hash
    forwarded = quota.identify(request(ip="2001:db8:abcd:1::1", forwarded="198.51.100.9"))
    assert forwarded.ip_hash == first.ip_hash


def test_trusted_proxy_uses_first_forwarded_address(
    clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(quota, "settings", replace(quota.settings, trusted_proxy=True))
    proxied = quota.identify(request(ip="127.0.0.1", forwarded="198.51.100.9, 127.0.0.1"))
    direct = quota.identify(request(ip="198.51.100.9"))
    assert proxied.ip_hash == direct.ip_hash


@pytest.mark.asyncio
async def test_purge_removes_old_rows_and_keeps_recent_usage(clock: list[float]) -> None:
    user = identity()
    await quota.reserve(user, 60, "old")
    await quota.reserve_budget(0.1)
    clock[0] += 4 * 86400
    await quota.reserve(user, 120, "recent")
    await quota.reserve_budget(0.2)
    await quota.purge()
    connection = sqlite3.connect(Path(quota.settings.data_dir) / "quota.sqlite3")
    try:
        assert connection.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM budget").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        connection.close()
    assert await quota.remaining_s(user) == 7080


@pytest.mark.asyncio
async def test_storage_contains_hashes_and_never_raw_identifiers(clock: list[float]) -> None:
    user = quota.identify(request(fingerprint="a" * 64, ip="198.51.100.3"))
    await quota.reserve(user, 60, "opaque-job-id")
    connection = sqlite3.connect(Path(quota.settings.data_dir) / "quota.sqlite3")
    try:
        dumped = "\n".join(connection.iterdump())
    finally:
        connection.close()
    assert user.cookie_value not in dumped
    assert "198.51.100.3" not in dumped
    assert "a" * 64 not in dumped
    assert user.cookie_hash in dumped
    assert user.fingerprint_hash in dumped
