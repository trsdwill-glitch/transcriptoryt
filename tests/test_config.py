from __future__ import annotations

from dataclasses import fields

import pytest

from app.config import Settings


@pytest.mark.parametrize("missing", ["SECRET_KEY", "HASH_SALT", "both"])
def test_production_requires_secrets(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    for item in fields(Settings):
        monkeypatch.delenv(item.name.upper(), raising=False)
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "" if missing in {"SECRET_KEY", "both"} else "secret")
    monkeypatch.setenv("HASH_SALT", " " if missing in {"HASH_SALT", "both"} else "salt")
    with pytest.raises(ValueError, match="SECRET_KEY and HASH_SALT"):
        Settings.from_env()


def test_production_accepts_configured_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    for item in fields(Settings):
        monkeypatch.delenv(item.name.upper(), raising=False)
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "secret")
    monkeypatch.setenv("HASH_SALT", "salt")
    configured = Settings.from_env()
    assert configured.env == "production"
    assert configured.secret_key == "secret"
