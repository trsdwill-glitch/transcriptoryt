from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    env: str = "development"
    provider: str = "groq"
    groq_api_key: str = ""
    groq_model: str = "whisper-large-v3-turbo"
    cf_account_id: str = ""
    cf_api_token: str = ""
    secret_key: str = ""
    hash_salt: str = ""
    data_dir: str = "./data"
    daily_quota_min: int = 120
    ip_quota_multiplier: int = 3
    max_file_min: int = 120
    max_items: int = 5
    max_upload_mb: int = 500
    max_download_mb: int = 500
    daily_budget_usd: float = 0.50
    cost_per_hour_usd: float = 0.04
    result_ttl_s: int = 3600
    chunk_seconds: int = 600
    max_concurrent_jobs: int = 2
    job_timeout_s: int = 3600
    provider_concurrency: int = 3
    trusted_proxy: bool = False
    ytdlp_hosts: list[str] = field(default_factory=lambda: [
        "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be",
    ])
    ytdlp_proxy: str = ""
    ytdlp_cookies_file: str = ""
    rate_limit_per_min: int = 60
    jobs_per_hour_per_ip: int = 10
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> Settings:
        defaults = cls()
        values: dict[str, str | int | float | bool | list[str]] = {}
        for item in fields(cls):
            raw = os.environ.get(item.name.upper())
            if raw is None:
                continue
            default = getattr(defaults, item.name)
            try:
                if isinstance(default, bool):
                    if raw.strip().lower() not in {"true", "false", "1", "0", "yes", "no"}:
                        raise ValueError
                    value: str | int | float | bool | list[str] = raw.strip().lower() in {"true", "1", "yes"}
                elif isinstance(default, int):
                    value = int(raw)
                elif isinstance(default, float):
                    value = float(raw)
                elif isinstance(default, list):
                    value = [host.strip().lower().rstrip(".") for host in raw.split(",") if host.strip()]
                else:
                    value = raw
            except ValueError:
                raise ValueError(f"Invalid configuration: {item.name.upper()}") from None
            values[item.name] = value
        result = cls(**values)
        result._validate()
        return result

    def _validate(self) -> None:
        import math

        if self.env == "production" and (not self.secret_key.strip() or not self.hash_salt.strip()):
            raise ValueError("SECRET_KEY and HASH_SALT must be configured in production")
        if self.provider not in {"groq", "cloudflare"}:
            raise ValueError("PROVIDER must be groq or cloudflare")
        for name in (
            "ip_quota_multiplier", "max_file_min", "max_items", "max_upload_mb",
            "max_download_mb", "result_ttl_s", "chunk_seconds", "max_concurrent_jobs",
            "job_timeout_s", "provider_concurrency", "rate_limit_per_min", "jobs_per_hour_per_ip",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name.upper()} must be positive")
        for name in ("daily_quota_min", "daily_budget_usd", "cost_per_hour_usd"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name.upper()} must be finite and non-negative")
        if not self.data_dir.strip():
            raise ValueError("DATA_DIR must not be empty")
        if self.log_level.upper() not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}:
            raise ValueError("LOG_LEVEL is invalid")


settings = Settings.from_env()


def ensure_dirs() -> None:
    for path in (Path(settings.data_dir), Path(settings.data_dir) / "tmp", Path(settings.data_dir) / "results"):
        path.mkdir(parents=True, exist_ok=True)
