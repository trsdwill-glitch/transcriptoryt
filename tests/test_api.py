from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import replace
import io
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from typing import Any, AsyncIterator, Callable
from urllib.parse import urlsplit
import zipfile

import httpx
import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def api(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[SimpleNamespace]:
    from app.models import AppError, AudioInfo, Chunk, RemoteProbe, RenderedFile, Segment, Transcript
    from app import config, jobs, main, quota, ratelimit

    configured = replace(
        config.settings, data_dir=str(tmp_path / "data"), secret_key="test-secret", hash_salt="test-salt",
        daily_quota_min=120, daily_budget_usd=10, rate_limit_per_min=10000,
        jobs_per_hour_per_ip=1000, result_ttl_s=3600, max_upload_mb=1,
    )
    for module in (config, jobs, main, quota, ratelimit):
        monkeypatch.setattr(module, "settings", configured)
    control = SimpleNamespace(
        duration=60.0, actual_duration=None, block=None, entered=asyncio.Event(), rendering=[], download_calls=0,
        fail_sources=set(), download_block=None, download_entered=asyncio.Event(),
    )
    security, ingest, transcribe, formats = (ModuleType(f"app.{name}") for name in ("security", "ingest", "transcribe", "formats"))

    def validate_url(url: str) -> str:
        if urlsplit(url).scheme not in {"http", "https"} or not urlsplit(url).hostname:
            raise AppError("URL_INVALID", "Invalid URL.")
        return url

    async def probe_url(url: str) -> RemoteProbe:
        return RemoteProbe("Remote audio", control.duration, "direct")

    async def probe_file(path: str) -> float:
        assert Path(path).is_file()
        return control.duration

    async def fetch_audio(url: str, workdir: str) -> AudioInfo:
        control.download_calls += 1
        control.download_entered.set()
        if control.download_block is not None:
            await control.download_block.wait()
        if url in control.fail_sources:
            raise AppError("DOWNLOAD_FAILED", "The media could not be downloaded.")
        path = Path(workdir) / "audio.mp3"
        path.write_bytes(b"audio")
        duration = control.actual_duration if control.actual_duration is not None else control.duration
        return AudioInfo(str(path), duration, "Remote audio", url)

    async def save_upload(tmp_path: str, original_name: str, workdir: str) -> AudioInfo:
        return AudioInfo(tmp_path, control.duration, Path(original_name).stem, original_name)

    async def make_chunks(info: AudioInfo, workdir: str) -> list[Chunk]:
        return [Chunk(info.path, 0, info.duration_s)]

    async def transcribe_chunks(
        info: AudioInfo, chunks: list[Chunk], language: str | None,
        on_progress: Callable[[float], None] | None = None,
    ) -> Transcript:
        control.entered.set()
        if control.block is not None:
            await control.block.wait()
        if on_progress is not None:
            on_progress(1.0)
        return Transcript(info.title, info.source, language or "en", info.duration_s, [Segment(0, info.duration_s, "Hello world")])

    def estimate_cost_usd(duration_s: float) -> float:
        return duration_s / 3600 * configured.cost_per_hour_usd

    def render(transcript: Transcript, fmt: str, timestamps: bool) -> RenderedFile:
        control.rendering.append("single")
        return RenderedFile(f"résumé.{fmt}", "text/plain", b"Hello world")

    def render_combined(transcripts: list[Transcript], fmt: str, timestamps: bool) -> RenderedFile:
        control.rendering.append("combined")
        return RenderedFile(f"combined.{fmt}", "text/plain", b"Hello world\nHello world")

    def render_zip(transcripts: list[Transcript], fmt: str, timestamps: bool) -> RenderedFile:
        control.rendering.append("zip")
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            for index, transcript in enumerate(transcripts):
                archive.writestr(f"{index}.{fmt}", "Hello world")
        return RenderedFile("transcripts.zip", "application/zip", output.getvalue())

    for module, attributes in (
        (security, {"validate_url": validate_url}),
        (ingest, {"probe_url": probe_url, "probe_file": probe_file, "fetch_audio": fetch_audio, "save_upload": save_upload, "make_chunks": make_chunks}),
        (transcribe, {"transcribe_chunks": transcribe_chunks, "estimate_cost_usd": estimate_cost_usd}),
        (formats, {"FORMATS": main.FORMATS, "COMBINABLE": main.COMBINABLE, "render": render, "render_combined": render_combined, "render_zip": render_zip}),
    ):
        for name, function in attributes.items():
            setattr(module, name, function)
        monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(main, "manager", jobs.JobManager())
    test_limiter = ratelimit.RateLimiter()
    monkeypatch.setattr(ratelimit, "limiter", test_limiter)
    monkeypatch.setattr(main, "limiter", test_limiter)
    async with main.app.router.lifespan_context(main.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://testserver") as client:
            yield SimpleNamespace(
                client=client, control=control, main=main, jobs=jobs, quota=quota, settings=configured,
                tmp_path=tmp_path, ingest=ingest, transcribe=transcribe, formats=formats,
            )


async def submit(
    client: httpx.AsyncClient, urls: list[str] | None = None, fmt: str = "txt", combine: bool = False,
    uploads: list[tuple[str, bytes]] | None = None,
) -> httpx.Response:
    fields: list[tuple[str, Any]] = [
        ("format", (None, fmt)), ("combine", (None, str(combine).lower())),
        ("urls", (None, json.dumps(urls or []))),
    ]
    fields.extend(("files", (name, data, "audio/mpeg")) for name, data in uploads or [])
    return await client.post("/api/jobs", files=fields)


async def finished(client: httpx.AsyncClient, job_id: str) -> dict[str, Any]:
    for attempt in range(300):
        response = await client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200, response.text
        value = response.json()
        if value["status"] in {"done", "error", "cancelled"}:
            return value
        await asyncio.sleep(0.01)
    pytest.fail("Job did not finish")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("count", "fmt", "combine", "mode"),
    [(1, "txt", False, "single"), (2, "txt", False, "zip"), (2, "txt", True, "combined"), (2, "srt", True, "zip")],
)
async def test_complete_pipeline(api: SimpleNamespace, count: int, fmt: str, combine: bool, mode: str) -> None:
    response = await submit(api.client, [f"https://example.com/{index}.mp3" for index in range(count)], fmt, combine)
    assert response.status_code == 202, response.text
    created = response.json()
    assert len(created["items"]) == count
    assert all(item["status"] == "pending" for item in created["items"])
    result = await finished(api.client, created["job_id"])
    assert result["status"] == "done", result
    assert result["progress"] == 1
    assert api.control.rendering == [mode]
    assert result["quota"]["used_min"] == pytest.approx(count)
    download = await api.client.get(result["result"]["download_url"])
    assert download.status_code == 200
    assert len(download.content) == result["result"]["size"]
    if mode == "zip":
        with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
            assert len(archive.namelist()) == count
    else:
        assert "Hello world" in download.text
    if mode == "single":
        assert "filename*=UTF-8''r%C3%A9sum%C3%A9.txt" in download.headers["content-disposition"]
    assert not (Path(api.settings.data_dir) / "tmp" / created["job_id"]).exists()


@pytest.mark.asyncio
async def test_upload_is_processed_and_removed(api: SimpleNamespace) -> None:
    response = await submit(api.client, uploads=[("recording.mp3", b"audio")])
    assert response.status_code == 202, response.text
    result = await finished(api.client, response.json()["job_id"])
    assert result["status"] == "done", result
    assert result["items"][0]["source"] == "recording.mp3"
    assert api.control.download_calls == 0


@pytest.mark.asyncio
async def test_cancel_refunds_and_blocks_duplicate_active_job(api: SimpleNamespace) -> None:
    await api.client.get("/api/health")
    api.control.block = asyncio.Event()
    response = await submit(api.client, ["https://example.com/a.mp3"])
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    await asyncio.wait_for(api.control.entered.wait(), 3)
    duplicate = await submit(api.client, ["https://example.com/b.mp3"])
    assert duplicate.json()["error"]["code"] == "JOB_ACTIVE"
    cancelled = await api.client.delete(f"/api/jobs/{job_id}")
    assert cancelled.status_code == 200
    assert cancelled.json() == {"ok": True}
    result = await finished(api.client, job_id)
    assert result["status"] == "cancelled"
    assert result["quota"]["remaining_min"] == 120
    assert not (Path(api.settings.data_dir) / "tmp" / job_id).exists()


@pytest.mark.asyncio
async def test_quota_rejection_precedes_audio_download(api: SimpleNamespace) -> None:
    api.control.duration = 4000
    response = await submit(api.client, ["https://example.com/a.mp3", "https://example.com/b.mp3"])
    assert response.status_code == 202
    result = await finished(api.client, response.json()["job_id"])
    assert result["status"] == "error"
    assert result["error"]["code"] == "QUOTA_EXCEEDED"
    assert api.control.download_calls == 0
    assert result["quota"]["remaining_min"] == 120


@pytest.mark.asyncio
async def test_batch_too_large(api: SimpleNamespace) -> None:
    response = await submit(api.client, [f"https://example.com/{index}.mp3" for index in range(6)])
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BATCH_TOO_LARGE"
    assert response.json()["error"]["params"] == {"max_items": 5}
    assert not list((Path(api.settings.data_dir) / "tmp").iterdir())


@pytest.mark.asyncio
async def test_expired_download_returns_contract_error(api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    response = await submit(api.client, ["https://example.com/a.mp3"])
    job_id = response.json()["job_id"]
    result = await finished(api.client, job_id)
    assert result["status"] == "done"
    now = api.jobs.time.time()
    monkeypatch.setattr(api.jobs.time, "time", lambda: now + 3601)
    expired = await api.client.get(f"/api/jobs/{job_id}/download")
    assert expired.status_code == 404
    assert expired.json()["error"]["code"] == "RESULT_EXPIRED"


@pytest.mark.asyncio
async def test_oversized_upload_is_rejected_and_cleaned(api: SimpleNamespace) -> None:
    response = await submit(api.client, uploads=[("recording.mp3", b"x" * (1024 * 1024 + 1))])
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "TOO_LARGE"
    assert not list((Path(api.settings.data_dir) / "tmp").iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "code"),
    [("urls", "broken", "URL_INVALID"), ("urls", '[123]', "URL_INVALID"), ("urls", '"a"', "URL_INVALID"),
     ("format", "pdf", "BAD_FORMAT"), ("timestamps", "maybe", "BAD_FORMAT"), ("language", "invalid", "BAD_FORMAT")],
)
async def test_invalid_fields_have_safe_error_envelope(api: SimpleNamespace, field: str, value: str, code: str) -> None:
    response = await api.client.post("/api/jobs", files=[(field, (None, value))])
    assert response.status_code == 400
    assert response.json()["error"]["code"] == code
    assert set(response.json()["error"]) == {"code", "message", "params"}
    assert not list((Path(api.settings.data_dir) / "tmp").iterdir())


@pytest.mark.asyncio
async def test_cookie_security_headers_and_private_job_access(api: SimpleNamespace) -> None:
    response = await api.client.get("/api/health")
    assert response.json() == {"ok": True, "version": "1"}
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Max-Age=31536000" in cookie
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    config = (await api.client.get("/api/config")).json()
    assert config["formats"] == ["txt", "md", "docx", "srt", "vtt"]
    response = await submit(api.client, ["https://example.com/a.mp3"])
    job_id = response.json()["job_id"]
    await finished(api.client, job_id)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.main.app), base_url="http://testserver") as stranger:
        for method, path in (("GET", f"/api/jobs/{job_id}"), ("GET", f"/api/jobs/{job_id}/download"), ("DELETE", f"/api/jobs/{job_id}")):
            denied = await stranger.request(method, path)
            assert denied.status_code == 404
            assert denied.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_rate_limit_has_contract_error(api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    def rejected(ip: str) -> None:
        from app.models import AppError
        raise AppError("RATE_LIMITED", "Too many requests.", http_status=429)

    monkeypatch.setattr(api.main.limiter, "check_request", rejected)
    response = await api.client.get("/api/health")
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "RATE_LIMITED"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.asyncio
async def test_partial_failure_keeps_success_and_refunds_failed_item(api: SimpleNamespace) -> None:
    api.control.fail_sources.add("https://example.com/bad.mp3")
    response = await submit(api.client, ["https://example.com/good.mp3", "https://example.com/bad.mp3"])
    result = await finished(api.client, response.json()["job_id"])
    assert result["status"] == "done", result
    assert result["items"][0]["status"] == "done"
    assert result["items"][1]["status"] == "error"
    assert result["items"][1]["error"]["code"] == "DOWNLOAD_FAILED"
    assert result["quota"]["used_min"] == 1
    assert result["result"] is not None


@pytest.mark.asyncio
async def test_budget_rejection_refunds_quota_before_download(api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api.quota, "settings", replace(api.settings, daily_budget_usd=0.0001))
    response = await submit(api.client, ["https://example.com/a.mp3"])
    result = await finished(api.client, response.json()["job_id"])
    assert result["status"] == "error"
    assert result["error"]["code"] == "BUDGET_EXCEEDED"
    assert result["quota"]["used_min"] == 0
    assert api.control.download_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(("estimated", "actual"), [(120, 60), (60, 120)])
async def test_real_duration_adjusts_reservation(api: SimpleNamespace, estimated: float, actual: float) -> None:
    api.control.duration = estimated
    api.control.actual_duration = actual
    response = await submit(api.client, ["https://example.com/a.mp3"])
    result = await finished(api.client, response.json()["job_id"])
    assert result["status"] == "done", result
    assert result["quota"]["used_min"] == actual / 60


@pytest.mark.asyncio
async def test_real_duration_cannot_exceed_available_quota(api: SimpleNamespace) -> None:
    api.control.duration = 60
    api.control.actual_duration = 4000
    response = await submit(api.client, ["https://example.com/a.mp3", "https://example.com/b.mp3"])
    result = await finished(api.client, response.json()["job_id"])
    assert result["status"] == "done", result
    assert result["items"][0]["status"] == "done"
    assert result["items"][1]["error"]["code"] == "QUOTA_EXCEEDED"
    assert result["quota"]["used_min"] == pytest.approx(4000 / 60)


@pytest.mark.asyncio
async def test_unknown_duration_reserves_maximum_before_download(api: SimpleNamespace) -> None:
    api.control.duration = None
    api.control.actual_duration = 60
    api.control.download_block = asyncio.Event()
    response = await submit(api.client, ["https://example.com/a.mp3"])
    job_id = response.json()["job_id"]
    await asyncio.wait_for(api.control.download_entered.wait(), 3)
    during = await api.client.get("/api/quota")
    assert during.json()["used_min"] == 120
    api.control.download_block.set()
    result = await finished(api.client, job_id)
    assert result["status"] == "done", result
    assert result["quota"]["used_min"] == 1


@pytest.mark.asyncio
async def test_job_timeout_refunds_and_removes_temporary_audio(api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api.jobs, "settings", replace(api.settings, job_timeout_s=1))
    api.control.block = asyncio.Event()
    response = await submit(api.client, ["https://example.com/a.mp3"])
    job_id = response.json()["job_id"]
    result = await finished(api.client, job_id)
    assert result["status"] == "error"
    assert result["error"]["code"] == "TIMEOUT"
    assert result["quota"]["used_min"] == 0
    assert not (Path(api.settings.data_dir) / "tmp" / job_id).exists()


@pytest.mark.asyncio
async def test_queued_job_cancellation_does_not_wait_for_running_job(api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    await api.main.manager.close()
    monkeypatch.setattr(api.jobs, "settings", replace(api.settings, max_concurrent_jobs=1))
    await api.main.manager.start()
    api.control.block = asyncio.Event()
    response = await submit(api.client, ["https://example.com/a.mp3"])
    running_id = response.json()["job_id"]
    await asyncio.wait_for(api.control.entered.wait(), 3)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.main.app), base_url="http://testserver") as second:
        queued = await submit(second, ["https://example.com/b.mp3"])
        queued_id = queued.json()["job_id"]
        snapshot = (await second.get(f"/api/jobs/{queued_id}")).json()
        assert snapshot["status"] == "queued"
        assert snapshot["queue_position"] >= 1
        response = await asyncio.wait_for(second.delete(f"/api/jobs/{queued_id}"), 2)
        assert response.status_code == 200
        result = await finished(second, queued_id)
        assert result["status"] == "cancelled"
        assert not (Path(api.settings.data_dir) / "tmp" / queued_id).exists()
    api.control.block.set()
    assert (await finished(api.client, running_id))["status"] == "done"


@pytest.mark.asyncio
async def test_https_cookie_is_secure(api: SimpleNamespace) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.main.app), base_url="https://testserver") as client:
        response = await client.get("/api/health")
        assert response.status_code == 200
        assert "; Secure" in response.headers["set-cookie"]


@pytest.mark.asyncio
async def test_https_requires_configured_secrets(api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api.main, "settings", replace(api.settings, secret_key="", hash_salt=""))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.main.app), base_url="https://testserver") as client:
        response = await client.get("/api/health")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "INTERNAL"
        assert "set-cookie" not in response.headers


@pytest.mark.asyncio
async def test_concurrent_expiration_is_idempotent(api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    response = await submit(api.client, ["https://example.com/a.mp3"])
    job_id = response.json()["job_id"]
    result = await finished(api.client, job_id)
    assert result["status"] == "done"
    expired_at = api.jobs.time.time() + api.settings.result_ttl_s + 1
    monkeypatch.setattr(api.jobs, "time", SimpleNamespace(time=lambda: expired_at))
    responses = await asyncio.gather(*(
        api.client.get(f"/api/jobs/{job_id}/download") for index in range(8)
    ))
    assert all(response.status_code == 404 for response in responses)
    assert all(response.json()["error"]["code"] == "RESULT_EXPIRED" for response in responses)
    assert not (Path(api.settings.data_dir) / "results" / job_id).exists()


@pytest.mark.asyncio
async def test_restart_cleans_orphan_audio_and_expired_results(api: SimpleNamespace) -> None:
    import os

    await api.main.manager.close()
    root = Path(api.settings.data_dir)
    abandoned_audio = root / "tmp" / "previous-process"
    abandoned_audio.mkdir()
    (abandoned_audio / "audio.mp3").write_bytes(b"audio")
    expired = root / "results" / "expired-result"
    fresh = root / "results" / "fresh-result"
    for directory in (expired, fresh):
        directory.mkdir()
        (directory / "result").write_bytes(b"result")
    timestamp = api.jobs.time.time() - api.settings.result_ttl_s - 1
    os.utime(expired, (timestamp, timestamp))
    await api.main.manager.start()
    await api.main.manager.sweep()
    assert not abandoned_audio.exists()
    assert not expired.exists()
    assert (fresh / "result").is_file()


@pytest.mark.asyncio
async def test_partial_failure_refunds_its_own_utc_budget_days(
    api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime, timezone
    import sqlite3

    from app.models import AppError, AudioInfo, Chunk, Transcript

    now = [datetime(2026, 11, 22, 23, 59, 50, tzinfo=timezone.utc).timestamp()]
    clock = SimpleNamespace(time=lambda: now[0])
    monkeypatch.setattr(api.quota, "time", clock)
    monkeypatch.setattr(api.jobs, "time", clock)
    original_fetch = api.ingest.fetch_audio
    original_transcribe = api.transcribe.transcribe_chunks

    async def fetch(url: str, workdir: str) -> AudioInfo:
        info = await original_fetch(url, workdir)
        if url.endswith("bad.mp3"):
            now[0] += 20
            info.duration_s = 120
        return info

    async def transcribe(
        info: AudioInfo, chunks: list[Chunk], language: str | None,
        on_progress: Callable[[float], None] | None = None,
    ) -> Transcript:
        if info.source.endswith("bad.mp3"):
            raise AppError("PROVIDER_ERROR", "The provider could not transcribe this item.")
        return await original_transcribe(info, chunks, language, on_progress)

    monkeypatch.setattr(api.ingest, "fetch_audio", fetch)
    monkeypatch.setattr(api.transcribe, "transcribe_chunks", transcribe)
    response = await submit(api.client, ["https://example.com/good.mp3", "https://example.com/bad.mp3"])
    result = await finished(api.client, response.json()["job_id"])
    assert result["status"] == "done", result
    assert result["quota"]["used_min"] == 1

    def stored_budgets() -> dict[str, float]:
        with closing(sqlite3.connect(Path(api.settings.data_dir) / "quota.sqlite3")) as connection:
            return dict(connection.execute("SELECT day, usd FROM budget"))

    budgets = await asyncio.to_thread(stored_budgets)
    assert budgets["2026-11-22"] == pytest.approx(60 / 3600 * api.settings.cost_per_hour_usd)
    assert budgets["2026-11-23"] == pytest.approx(0)


@pytest.mark.asyncio
async def test_malformed_multipart_does_not_log_request_content(api: SimpleNamespace, caplog: pytest.LogCaptureFixture) -> None:
    response = await api.client.post(
        "/api/jobs", content=b"private audio content",
        headers={"Content-Type": "multipart/form-data; boundary=expected"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BAD_FORMAT"
    assert "private audio content" not in caplog.text
    assert not list((Path(api.settings.data_dir) / "tmp").iterdir())
