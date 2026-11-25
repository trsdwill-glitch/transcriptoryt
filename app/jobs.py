from __future__ import annotations

import asyncio
import importlib
import logging
import math
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from app import quota
from app.config import settings
from app.models import AppError, AudioInfo, RenderedFile, Transcript

logger = logging.getLogger(__name__)
_T = TypeVar("_T")
_TERMINAL = {"done", "error", "cancelled"}


def _error(error: AppError) -> dict[str, Any]:
    return {"code": error.code, "message": error.message, "params": error.params}


def _remove_directory(path: Path, root: Path) -> None:
    # Only remove a direct child of the intended storage directory.
    if path.is_symlink():
        if path.absolute().parent != root.absolute():
            raise ValueError("Invalid storage directory")
        path.unlink(missing_ok=True)
        return
    if path.resolve().parent != root.resolve():
        raise ValueError("Invalid storage directory")
    if path.exists():
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            pass


@dataclass
class JobInput:
    source: str
    upload_path: str | None = None


@dataclass
class JobItem:
    index: int
    source: str
    upload_path: str | None = None
    title: str | None = None
    duration_s: float | None = None
    status: str = "pending"
    error: dict[str, Any] | None = None
    reserved_s: float = 0.0
    reserved_usd: float = 0.0
    budget: dict[str, float] = field(default_factory=dict)

    def view(self) -> dict[str, Any]:
        return {
            "index": self.index, "source": self.source, "title": self.title,
            "duration_s": self.duration_s, "status": self.status, "error": self.error,
        }


@dataclass
class Job:
    job_id: str
    identity: quota.Identity
    items: list[JobItem]
    fmt: str
    timestamps: bool
    combine: bool
    language: str | None
    workdir: Path
    result_dir: Path
    status: str = "queued"
    progress: float = 0.0
    error: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    result_path: Path | None = None
    created_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    reserved_s: float = 0.0
    budget: dict[str, float] = field(default_factory=dict)
    task: asyncio.Task[None] | None = None
    cancel_requested: bool = False
    result_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


async def _settle(operation: Awaitable[_T], committed: Callable[[_T], None] | None = None) -> _T:
    # A cancelled SQLite/file thread must finish before local accounting or cleanup.
    task = asyncio.ensure_future(operation)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
        except Exception:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
    if committed is not None:
        committed(result)
    if cancelled:
        raise asyncio.CancelledError
    return result


class JobManager:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self._active: dict[str, str] = {}
        self._queue: list[str] = []
        self._semaphore: asyncio.Semaphore | None = None
        self._sweeper: asyncio.Task[None] | None = None
        self._closing = False

    async def start(self) -> None:
        self._closing = False
        await asyncio.to_thread(self._clean_orphan_uploads)
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(settings.max_concurrent_jobs)
        if self._sweeper is None or self._sweeper.done():
            self._sweeper = asyncio.create_task(self._sweep_loop(), name="result-sweeper")

    async def close(self) -> None:
        self._closing = True
        if self._sweeper is not None:
            self._sweeper.cancel()
            await asyncio.gather(self._sweeper, return_exceptions=True)
            self._sweeper = None
        tasks: list[asyncio.Task[None]] = []
        for job in self.jobs.values():
            if job.task is not None and not job.task.done():
                job.cancel_requested = True
                tasks.append(job.task)
        if tasks:
            # Let newly submitted tasks enter their cleanup scope.
            await asyncio.sleep(0)
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        self._semaphore = None

    def assert_available(self, identity: quota.Identity) -> None:
        if self._closing:
            raise AppError("UNAVAILABLE", "The service is shutting down.", http_status=503)
        if identity.cookie_hash in self._active:
            raise AppError("JOB_ACTIVE", "You already have an active job.", http_status=409)

    def submit(
        self, job_id: str, identity: quota.Identity, inputs: list[JobInput],
        fmt: str, timestamps: bool, combine: bool, language: str | None,
    ) -> dict[str, Any]:
        self.assert_available(identity)
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(settings.max_concurrent_jobs)
        job = Job(
            job_id=job_id, identity=identity,
            items=[JobItem(index=i, source=item.source, upload_path=item.upload_path)
                   for i, item in enumerate(inputs)],
            fmt=fmt, timestamps=timestamps, combine=combine, language=language,
            workdir=Path(settings.data_dir) / "tmp" / job_id,
            result_dir=Path(settings.data_dir) / "results" / job_id,
        )
        self.jobs[job_id] = job
        self._active[identity.cookie_hash] = job_id
        self._queue.append(job_id)
        job.task = asyncio.create_task(self._run(job), name=f"job-{job_id[:8]}")
        return {"job_id": job_id, "items": [item.view() for item in job.items]}

    def _get(self, job_id: str, identity: quota.Identity) -> Job:
        job = self.jobs.get(job_id)
        if job is None or job.identity.cookie_hash != identity.cookie_hash:
            raise AppError("NOT_FOUND", "Job not found.", http_status=404)
        return job

    async def snapshot(self, job_id: str, identity: quota.Identity) -> dict[str, Any]:
        job = self._get(job_id, identity)
        if job.status in _TERMINAL and job.task is not None and not job.task.done():
            await asyncio.shield(job.task)
        await self._expire_result(job)
        position = self._queue.index(job_id) + 1 if job_id in self._queue else 0
        return {
            "status": job.status, "progress": job.progress,
            "queue_position": position, "items": [item.view() for item in job.items],
            "result": job.result if job.status == "done" else None, "error": job.error,
            "quota": await quota.quota_view(identity),
        }

    async def cancel(self, job_id: str, identity: quota.Identity) -> None:
        job = self._get(job_id, identity)
        if job.status in _TERMINAL:
            return
        job.cancel_requested = True
        if job.task is not None:
            await asyncio.sleep(0)
            if not job.task.done():
                job.task.cancel()
            await asyncio.gather(job.task, return_exceptions=True)

    async def download(self, job_id: str, identity: quota.Identity) -> tuple[Path, str, str]:
        job = self._get(job_id, identity)
        await self._expire_result(job)
        if job.status != "done":
            raise AppError("NOT_FOUND", "Result not found.", http_status=404)
        if job.status == "done" and (job.result is None or job.result_path is None):
            raise AppError("RESULT_EXPIRED", "This result has expired.", http_status=404)
        if job.result is None or job.result_path is None or not job.result_path.is_file():
            raise AppError("NOT_FOUND", "Result not found.", http_status=404)
        return job.result_path, job.result["filename"], job.result["mime"]

    @staticmethod
    def _check_cancelled(job: Job) -> None:
        if job.cancel_requested:
            raise asyncio.CancelledError

    @staticmethod
    def _state(job: Job, status: str) -> None:
        job.status = status
        logger.info("job=%s state=%s", job.job_id[:8], status)

    @staticmethod
    def _duration(value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise AppError("UNSUPPORTED_FILE", "The audio duration is unavailable.")
        if value > settings.max_file_min * 60:
            raise AppError("TOO_LONG", "The audio is too long.", {"max_min": settings.max_file_min})
        return value

    async def _add_quota(self, job: Job, seconds: float) -> None:
        def committed(_: None) -> None:
            job.reserved_s += seconds
        await _settle(quota.reserve(job.identity, seconds, job.job_id), committed)

    async def _subtract_quota(self, job: Job, seconds: float) -> None:
        seconds = min(seconds, job.reserved_s)
        if seconds <= 0:
            return
        def committed(_: None) -> None:
            job.reserved_s = max(0.0, job.reserved_s - seconds)
        await _settle(quota.refund(job.identity, seconds, job.job_id), committed)

    async def _add_budget(self, job: Job, usd: float) -> str | None:
        if usd <= 0:
            return None
        day = quota.budget_day()
        def committed(_: None) -> None:
            job.budget[day] = job.budget.get(day, 0.0) + usd
        await _settle(quota.reserve_budget(usd, day=day), committed)
        return day

    async def _subtract_budget(self, job: Job, usd: float, *, only_day: str | None = None) -> None:
        for day, reserved in list(job.budget.items()):
            if only_day is not None and day != only_day:
                continue
            amount = min(usd, reserved)
            if amount <= 0:
                continue
            def committed(_: None, *, key: str = day, value: float = amount) -> None:
                job.budget[key] = max(0.0, job.budget[key] - value)
            await _settle(quota.refund_budget(amount, day=day), committed)
            usd = max(0.0, usd - amount)

    async def _subtract_item_budget(self, job: Job, item: JobItem, usd: float) -> None:
        for day, reserved in list(item.budget.items()):
            amount = min(usd, reserved)
            if amount > 0:
                await self._subtract_budget(job, amount, only_day=day)
                item.budget[day] = max(0.0, item.budget[day] - amount)
                usd = max(0.0, usd - amount)

    async def _release_item(self, job: Job, item: JobItem) -> None:
        await self._subtract_quota(job, item.reserved_s)
        item.reserved_s = 0.0
        await self._subtract_item_budget(job, item, item.reserved_usd)
        item.reserved_usd = 0.0

    async def _adjust_item(self, job: Job, item: JobItem, seconds: float, usd: float) -> None:
        delta_s, delta_usd = seconds - item.reserved_s, usd - item.reserved_usd
        if delta_s > 0:
            await self._add_quota(job, delta_s)
            item.reserved_s += delta_s
        elif delta_s < 0:
            await self._subtract_quota(job, -delta_s)
            item.reserved_s += delta_s
        if delta_usd > 0:
            day = await self._add_budget(job, delta_usd)
            if day is not None:
                item.budget[day] = item.budget.get(day, 0.0) + delta_usd
        elif delta_usd < 0:
            await self._subtract_item_budget(job, item, -delta_usd)
        item.reserved_usd = usd

    async def _pipeline(self, job: Job) -> None:
        ingest = importlib.import_module("app.ingest")
        transcribe = importlib.import_module("app.transcribe")
        formats = importlib.import_module("app.formats")
        self._check_cancelled(job)
        self._state(job, "checking")
        await _settle(asyncio.to_thread(job.workdir.mkdir, parents=True, exist_ok=True))
        valid: list[JobItem] = []
        for item in job.items:
            self._check_cancelled(job)
            try:
                if item.upload_path is None:
                    probe = await ingest.probe_url(item.source)
                    item.title = probe.title
                    duration = probe.duration_s
                else:
                    duration = await ingest.probe_file(item.upload_path)
                    item.title = Path(item.source.replace("\\", "/")).stem
                # Unknown remote durations reserve the maximum before downloading.
                item.duration_s = self._duration(float(duration)) if duration is not None else None
                item.reserved_s = item.duration_s if item.duration_s is not None else settings.max_file_min * 60
                item.reserved_usd = transcribe.estimate_cost_usd(item.reserved_s)
                valid.append(item)
            except AppError as error:
                item.status, item.error = "error", _error(error)
            except Exception:
                item.status, item.error = "error", _error(AppError("INTERNAL", "Could not inspect this item.", http_status=500))
        if not valid:
            first = next(item.error for item in job.items if item.error is not None)
            raise AppError(first["code"], first["message"], first["params"])
        await self._add_quota(job, sum(item.reserved_s for item in valid))
        day = await self._add_budget(job, sum(item.reserved_usd for item in valid))
        if day is not None:
            for item in valid:
                item.budget[day] = item.reserved_usd
        transcripts: list[Transcript] = []
        total = len(job.items)
        for item in valid:
            self._check_cancelled(job)
            try:
                self._state(job, "downloading")
                item.status = "downloading"
                if item.upload_path is None:
                    info: AudioInfo = await ingest.fetch_audio(item.source, str(job.workdir))
                else:
                    info = await ingest.save_upload(item.upload_path, item.source, str(job.workdir))
                duration = self._duration(float(info.duration_s))
                await self._adjust_item(job, item, duration, transcribe.estimate_cost_usd(duration))
                item.title, item.duration_s = info.title, duration
                self._check_cancelled(job)
                chunks = await ingest.make_chunks(info, str(job.workdir))
                self._check_cancelled(job)
                self._state(job, "transcribing")
                item.status = "transcribing"
                base = sum(entry.status in {"done", "error"} for entry in job.items)
                def on_progress(value: float, *, completed: int = base) -> None:
                    if math.isfinite(value):
                        job.progress = max(job.progress, min(0.94, 0.05 + 0.89 * (completed + max(0.0, min(1.0, value))) / total))
                transcript = await transcribe.transcribe_chunks(info, chunks, job.language, on_progress)
                self._check_cancelled(job)
                transcripts.append(transcript)
                item.status = "done"
            except AppError as error:
                item.status, item.error = "error", _error(error)
                await self._release_item(job, item)
                logger.info("job=%s item=%d code=%s", job.job_id[:8], item.index, error.code)
            except Exception:
                item.status, item.error = "error", _error(AppError("INTERNAL", "Could not process this item.", http_status=500))
                await self._release_item(job, item)
                logger.info("job=%s item=%d code=INTERNAL", job.job_id[:8], item.index)
            job.progress = max(job.progress, 0.05 + 0.89 * sum(entry.status in {"done", "error"} for entry in job.items) / total)
        if not transcripts:
            first = next((item.error for item in job.items if item.error is not None), None)
            if first is not None:
                raise AppError(first["code"], first["message"], first["params"])
            raise AppError("PROVIDER_ERROR", "No items could be transcribed.")
        self._check_cancelled(job)
        self._state(job, "formatting")
        if len(transcripts) == 1:
            rendered = await _settle(asyncio.to_thread(formats.render, transcripts[0], job.fmt, job.timestamps))
        elif job.combine and job.fmt in formats.COMBINABLE:
            rendered = await _settle(asyncio.to_thread(formats.render_combined, transcripts, job.fmt, job.timestamps))
        else:
            rendered = await _settle(asyncio.to_thread(formats.render_zip, transcripts, job.fmt, job.timestamps))
        result_path, result = await _settle(asyncio.to_thread(self._write_result, job, rendered))
        self._check_cancelled(job)
        job.result_path, job.result = result_path, result
        job.progress = 1.0
        self._state(job, "done")

    @staticmethod
    def _write_result(job: Job, rendered: RenderedFile) -> tuple[Path, dict[str, Any]]:
        filename = Path(rendered.filename.replace("\\", "/")).name
        filename = "".join(char for char in filename if ord(char) >= 32 and ord(char) != 127)
        if filename in {"", ".", ".."}:
            filename = f"transcript.{job.fmt}"
        job.result_dir.mkdir(parents=True, exist_ok=True)
        # The display name is independent of the internal filesystem path.
        result_path = job.result_dir / "result"
        result_path.write_bytes(rendered.data)
        result = {
            "filename": filename, "mime": rendered.mime, "size": len(rendered.data),
            "download_url": f"/api/jobs/{job.job_id}/download",
        }
        return result_path, result

    async def _run(self, job: Job) -> None:
        try:
            async with asyncio.timeout(settings.job_timeout_s):
                self._check_cancelled(job)
                assert self._semaphore is not None
                async with self._semaphore:
                    self._queue.remove(job.job_id)
                    await self._pipeline(job)
        except asyncio.CancelledError:
            self._state(job, "cancelled")
            job.error = _error(AppError("CANCELLED", "The job was cancelled."))
        except TimeoutError:
            self._state(job, "error")
            job.error = _error(AppError("TIMEOUT", "The job took too long.", http_status=408))
        except AppError as error:
            self._state(job, "error")
            job.error = _error(error)
        except Exception:
            self._state(job, "error")
            job.error = _error(AppError("INTERNAL", "The job could not be completed.", http_status=500))
        finally:
            # Run cleanup separately so repeated cancellation cannot interrupt it.
            cleanup = asyncio.create_task(self._finish(job))
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            await cleanup

    async def _finish(self, job: Job) -> None:
        try:
            if job.status != "done":
                for action in (
                    self._subtract_quota(job, job.reserved_s),
                    self._subtract_budget(job, sum(job.budget.values())),
                ):
                    try:
                        await action
                    except Exception:
                        logger.error("job=%s code=REFUND_FAILED", job.job_id[:8])
                job.result, job.result_path = None, None
                for item in job.items:
                    if item.status != "error":
                        item.status = "error"
                        item.error = job.error
                try:
                    await asyncio.to_thread(_remove_directory, job.result_dir, job.result_dir.parent)
                except Exception:
                    logger.error("job=%s code=RESULT_CLEANUP_FAILED", job.job_id[:8])
        finally:
            try:
                await asyncio.to_thread(_remove_directory, job.workdir, job.workdir.parent)
            except Exception:
                logger.error("job=%s code=AUDIO_CLEANUP_FAILED", job.job_id[:8])
            finally:
                if job.job_id in self._queue:
                    self._queue.remove(job.job_id)
                if self._active.get(job.identity.cookie_hash) == job.job_id:
                    del self._active[job.identity.cookie_hash]
                job.completed_at = time.time()

    async def _expire_result(self, job: Job) -> None:
        if job.status == "done" and job.completed_at is not None and time.time() - job.completed_at >= settings.result_ttl_s:
            async with job.result_lock:
                job.result, job.result_path = None, None
                await asyncio.to_thread(_remove_directory, job.result_dir, job.result_dir.parent)

    def _clean_orphan_uploads(self) -> None:
        root = Path(settings.data_dir) / "tmp"
        if root.exists():
            for path in root.iterdir():
                if path.is_dir() and path.name not in self.jobs:
                    _remove_directory(path, root)

    def _clean_orphan_results(self, now: float) -> None:
        root = Path(settings.data_dir) / "results"
        if root.exists():
            for path in root.iterdir():
                if path.is_dir() and path.name not in self.jobs:
                    try:
                        expired = now - path.stat().st_mtime >= settings.result_ttl_s
                    except FileNotFoundError:
                        continue
                    if expired:
                        _remove_directory(path, root)

    async def sweep(self) -> None:
        now = time.time()
        for job_id, job in list(self.jobs.items()):
            await self._expire_result(job)
            if job.completed_at is not None and now - job.completed_at > max(settings.result_ttl_s, 25 * 3600):
                self.jobs.pop(job_id, None)
        await asyncio.to_thread(self._clean_orphan_results, now)
        await quota.purge()

    async def _sweep_loop(self) -> None:
        while True:
            try:
                await self.sweep()
            except Exception:
                logger.error("code=SWEEP_FAILED")
            await asyncio.sleep(300)
