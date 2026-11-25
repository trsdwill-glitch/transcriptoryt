from __future__ import annotations

import ipaddress
import math
import threading
import time
from collections import deque

from fastapi import Request

from app.config import settings
from app.models import AppError


def get_client_ip(request: Request) -> str:
    direct = request.client.host if request.client else "unknown"
    raw = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip() if settings.trusted_proxy else direct
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        try:
            address = ipaddress.ip_address(direct)
        except ValueError:
            return "unknown"
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


class RateLimiter:
    def __init__(self) -> None:
        self._requests: dict[str, deque[float]] = {}
        self._jobs: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _purge(entries: dict[str, deque[float]], cutoff: float) -> None:
        for ip in list(entries):
            timestamps = entries[ip]
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()
            if not timestamps:
                del entries[ip]

    def _check(self, ip: str, *, job: bool) -> None:
        now = time.monotonic()
        with self._lock:
            self._purge(self._requests, now - 60.0)
            self._purge(self._jobs, now - 3600.0)
            entries = self._jobs if job else self._requests
            limit = settings.jobs_per_hour_per_ip if job else settings.rate_limit_per_min
            window = 3600.0 if job else 60.0
            timestamps = entries.get(ip)
            if timestamps is not None and len(timestamps) >= limit:
                retry_after = max(1, math.ceil(timestamps[0] + window - now))
                raise AppError(
                    "RATE_LIMITED", "Too many requests. Please try again later.",
                    params={"resets_in_s": retry_after}, http_status=429,
                )
            if timestamps is None:
                timestamps = entries[ip] = deque()
            timestamps.append(now)

    def check_request(self, ip: str) -> None:
        self._check(ip, job=False)

    def check_job(self, ip: str) -> None:
        self._check(ip, job=True)


limiter = RateLimiter()
