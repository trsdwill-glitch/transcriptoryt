from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import replace
import importlib
import io
from pathlib import Path
import shutil
import sqlite3
from types import SimpleNamespace
from typing import Any, AsyncIterator
import wave
import zipfile

from docx import Document
import httpx
import pytest
import pytest_asyncio


pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="End-to-end media processing requires ffmpeg and ffprobe.",
)


def _wav() -> bytes:
    """Generate a one-second PCM recording without a binary fixture."""
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 16000)
    return output.getvalue()


@pytest_asyncio.fixture
async def integrated_api(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> AsyncIterator[SimpleNamespace]:
    modules = {
        name: importlib.import_module(f"app.{name}")
        for name in ("config", "jobs", "main", "quota", "ratelimit", "security", "ingest", "transcribe")
    }
    configured = replace(
        modules["config"].settings,
        data_dir=str(tmp_path / "data"),
        secret_key="e2e-secret", hash_salt="e2e-salt", trusted_proxy=False,
        daily_quota_min=120, daily_budget_usd=10, rate_limit_per_min=10000,
        jobs_per_hour_per_ip=1000, result_ttl_s=3600, max_upload_mb=1,
    )
    for module in modules.values():
        monkeypatch.setattr(module, "settings", configured)
    main, jobs, ratelimit = (modules[name] for name in ("main", "jobs", "ratelimit"))
    monkeypatch.setattr(main, "manager", jobs.JobManager())
    limiter = ratelimit.RateLimiter()
    monkeypatch.setattr(main, "limiter", limiter)
    monkeypatch.setattr(ratelimit, "limiter", limiter)
    control = SimpleNamespace(calls=0, block=False, entered=asyncio.Event(), release=asyncio.Event())

    async def provider_response(request: httpx.Request) -> httpx.Response:
        assert request.url == modules["transcribe"].GROQ_URL
        assert request.headers["content-type"].startswith("multipart/form-data;")
        assert b'filename="audio.mp3"' in request.content
        assert len(request.content) > 1000
        control.calls += 1
        control.entered.set()
        if control.block:
            await control.release.wait()
        return httpx.Response(200, json={
            "language": "english", "duration": 1.0,
            "segments": [{"start": 0.0, "end": 0.8, "text": "Hello world"}],
        })

    provider = modules["transcribe"].GroqProvider(
        api_key="test-key", transport=httpx.MockTransport(provider_response),
    )
    monkeypatch.setattr(modules["transcribe"], "get_provider", lambda: provider)
    async with main.app.router.lifespan_context(main.app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://testserver",
        ) as client:
            yield SimpleNamespace(
                client=client, control=control, main=main, settings=configured,
                modules=modules, monkeypatch=monkeypatch,
            )


async def _submit(
    api: SimpleNamespace, *, fmt: str = "txt", timestamps: bool = False,
    count: int = 1, combine: bool = False,
) -> str:
    fields: list[tuple[str, Any]] = [
        ("format", (None, fmt)),
        ("timestamps", (None, str(timestamps).lower())),
        ("combine", (None, str(combine).lower())),
    ]
    fields.extend(("files", (f"recording-{index}.wav", _wav(), "audio/wav")) for index in range(count))
    response = await api.client.post("/api/jobs", files=fields)
    assert response.status_code == 202, response.text
    return response.json()["job_id"]


async def _finished(api: SimpleNamespace, job_id: str) -> dict[str, Any]:
    async with asyncio.timeout(30):
        while True:
            response = await api.client.get(f"/api/jobs/{job_id}")
            assert response.status_code == 200, response.text
            snapshot = response.json()
            if snapshot["status"] in {"done", "error", "cancelled"}:
                return snapshot
            await asyncio.sleep(0.01)


def _assert_clean(api: SimpleNamespace, job_id: str) -> None:
    assert not (Path(api.settings.data_dir) / "tmp" / job_id).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fmt", "timestamps"),
    [("txt", False), ("txt", True), ("md", True), ("srt", False), ("vtt", False), ("docx", True)],
)
async def test_upload_through_real_media_and_renderers(
    integrated_api: SimpleNamespace, fmt: str, timestamps: bool,
) -> None:
    api = integrated_api
    job_id = await _submit(api, fmt=fmt, timestamps=timestamps)
    result = await _finished(api, job_id)
    assert result["status"] == "done", result
    assert result["progress"] == 1
    assert result["items"][0]["duration_s"] == pytest.approx(1.0, abs=0.02)
    assert result["quota"]["used_min"] == pytest.approx(1 / 60, abs=0.001)
    assert api.control.calls == 1
    download = await api.client.get(result["result"]["download_url"])
    assert download.status_code == 200
    assert len(download.content) == result["result"]["size"]
    assert f".{fmt}" in download.headers["content-disposition"]
    if fmt == "docx":
        text = "\n".join(paragraph.text for paragraph in Document(io.BytesIO(download.content)).paragraphs)
        assert "Hello world" in text
        assert "[00:00]" in text
    else:
        assert "Hello world" in download.text
        if fmt in {"txt", "md"}:
            assert ("[00:00]" in download.text) is timestamps
        elif fmt == "srt":
            assert "00:00:00,000 --> 00:00:00,800" in download.text
        elif fmt == "vtt":
            assert download.text.startswith("WEBVTT\n")
            assert "00:00:00.000 --> 00:00:00.800" in download.text
    _assert_clean(api, job_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fmt", "combine"), [("txt", True), ("md", True), ("docx", True), ("txt", False), ("srt", True)],
)
async def test_two_uploads_combined_or_zipped(
    integrated_api: SimpleNamespace, fmt: str, combine: bool,
) -> None:
    api = integrated_api
    job_id = await _submit(api, fmt=fmt, count=2, combine=combine)
    result = await _finished(api, job_id)
    assert result["status"] == "done", result
    assert all(item["status"] == "done" for item in result["items"])
    assert api.control.calls == 2
    download = await api.client.get(result["result"]["download_url"])
    assert download.status_code == 200
    if not combine or fmt == "srt":
        assert result["result"]["mime"] == "application/zip"
        with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
            names = archive.namelist()
            assert len(names) == len(set(names)) == 2
            assert all(name.endswith(f".{fmt}") for name in names)
            assert all("Hello world" in archive.read(name).decode() for name in names)
    else:
        if fmt == "docx":
            text = "\n".join(paragraph.text for paragraph in Document(io.BytesIO(download.content)).paragraphs)
        else:
            text = download.text
        assert text.count("Hello world") == 2
        assert "recording-0" in text and "recording-1" in text
    _assert_clean(api, job_id)


@pytest.mark.asyncio
async def test_quota_rejection_never_calls_provider_and_removes_upload(
    integrated_api: SimpleNamespace,
) -> None:
    api = integrated_api
    exhausted = replace(api.settings, daily_quota_min=0)
    for module in api.modules.values():
        api.monkeypatch.setattr(module, "settings", exhausted)
    job_id = await _submit(api)
    result = await _finished(api, job_id)
    assert result["status"] == "error"
    assert result["error"]["code"] == "QUOTA_EXCEEDED"
    assert api.control.calls == 0
    assert result["quota"]["used_min"] == 0
    _assert_clean(api, job_id)


@pytest.mark.asyncio
async def test_cancel_during_provider_request_refunds_and_removes_media(
    integrated_api: SimpleNamespace,
) -> None:
    api = integrated_api
    api.control.block = True
    job_id = await _submit(api)
    await asyncio.wait_for(api.control.entered.wait(), timeout=30)
    response = await api.client.delete(f"/api/jobs/{job_id}")
    assert response.status_code == 200, response.text
    result = await _finished(api, job_id)
    assert result["status"] == "cancelled"
    assert result["error"]["code"] == "CANCELLED"
    assert result["quota"]["used_min"] == pytest.approx(0)
    assert result["result"] is None
    with closing(sqlite3.connect(Path(api.settings.data_dir) / "quota.sqlite3")) as database:
        assert database.execute("SELECT COALESCE(SUM(usd), 0) FROM budget").fetchone()[0] == pytest.approx(0)
    _assert_clean(api, job_id)
    assert not (Path(api.settings.data_dir) / "results" / job_id).exists()


@pytest.mark.asyncio
async def test_expired_result_is_deleted_and_download_returns_expired(
    integrated_api: SimpleNamespace,
) -> None:
    api = integrated_api
    job_id = await _submit(api)
    result = await _finished(api, job_id)
    assert result["status"] == "done", result
    result_dir = Path(api.settings.data_dir) / "results" / job_id
    assert result_dir.is_dir()
    job = api.main.manager.jobs[job_id]
    assert job.completed_at is not None
    job.completed_at -= api.settings.result_ttl_s + 1
    await api.main.manager.sweep()
    assert not result_dir.exists()
    response = await api.client.get(f"/api/jobs/{job_id}/download")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RESULT_EXPIRED"
    _assert_clean(api, job_id)
