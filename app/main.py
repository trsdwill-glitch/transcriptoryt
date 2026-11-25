from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from importlib import import_module
import json
import logging
from pathlib import Path
import re
import secrets
import shutil
from collections.abc import Callable
from typing import Any, AsyncIterator, BinaryIO, TypeVar
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from python_multipart.multipart import MultipartParser, parse_options_header
from starlette.exceptions import HTTPException
from app.site_pages import SitePages

from app.config import ensure_dirs, settings
from app.formats import COMBINABLE, FORMATS
from app.jobs import JobInput, JobManager
from app.models import AppError
from app import quota
from app.ratelimit import get_client_ip, limiter


logger = logging.getLogger(__name__)
_T = TypeVar("_T")
manager = JobManager()
LANGUAGES = (
    "auto", "en", "es", "pt", "fr", "de", "it", "hi", "af", "am", "ar", "as",
    "az", "ba", "be", "bg", "bn", "bo", "br", "bs", "ca", "cs", "cy", "da",
    "el", "et", "eu", "fa", "fi", "fo", "gl", "gu", "ha", "haw", "he", "hr",
    "ht", "hu", "hy", "id", "is", "ja", "jw", "ka", "kk", "km", "kn", "ko",
    "la", "lb", "ln", "lo", "lt", "lv", "mg", "mi", "mk", "ml", "mn", "mr",
    "ms", "mt", "my", "ne", "nl", "nn", "no", "oc", "pa", "pl", "ps", "ro",
    "ru", "sa", "sd", "si", "sk", "sl", "sn", "so", "sq", "sr", "su", "sv",
    "sw", "ta", "te", "tg", "th", "tk", "tl", "tr", "tt", "uk", "ur", "uz",
    "vi", "yi", "yo", "zh",
)


def error_response(error: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=error.http_status,
        content={"error": {"code": error.code, "message": error.message, "params": error.params}},
    )


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    if (settings.env == "production" or settings.trusted_proxy) and (
        not settings.secret_key.strip() or not settings.hash_salt.strip()
    ):
        raise RuntimeError("SECRET_KEY and HASH_SALT must be configured for production or a trusted proxy.")
    await _io(ensure_dirs)
    logging.getLogger("app").setLevel(settings.log_level)
    private_loggers = ("uvicorn.access", "httpx", "httpcore", "python_multipart", "multipart")
    for name in private_loggers:
        logging.getLogger(name).setLevel(logging.CRITICAL + 1)
    for name in tuple(logging.Logger.manager.loggerDict):
        if any(name == prefix or name.startswith(prefix + ".") for prefix in private_loggers):
            logging.getLogger(name).disabled = True
    await manager.start()
    try:
        yield
    finally:
        await manager.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


async def _io(function: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    # Finish disk operations before cancellation closes handles or removes files.
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return result


def _remove_workdir(workdir: Path) -> None:
    root = (Path(settings.data_dir) / "tmp").resolve()
    if workdir.resolve().parent != root or workdir.is_symlink():
        raise ValueError("Invalid upload storage directory")
    if workdir.exists():
        shutil.rmtree(workdir)


@app.middleware("http")
async def protect_request(request: Request, call_next: Any) -> Any:
    identity: quota.Identity | None = None
    try:
        if (request.url.scheme == "https" or settings.trusted_proxy) and (
            not settings.secret_key.strip() or not settings.hash_salt.strip()
        ):
            raise AppError("INTERNAL", "The service is not configured.", http_status=503)
        identity = quota.identify(request)
        request.state.identity = identity
        if request.url.path.startswith("/api/") or request.url.path == "/api":
            limiter.check_request(get_client_ip(request))
        response = await call_next(request)
    except AppError as error:
        response = error_response(error)
    except Exception:
        logger.error("request_failed code=INTERNAL")
        response = error_response(AppError("INTERNAL", "An internal error occurred.", http_status=500))
    if identity is not None:
        response.set_cookie(
            "tid", identity.cookie_value, max_age=365 * 24 * 3600,
            httponly=True, samesite="lax", secure=request.url.scheme == "https" or settings.trusted_proxy,
        )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Frame-Options"] = "DENY"
    if request.url.path == "/api" or request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Robots-Tag"] = "noindex"
    return response


@app.exception_handler(AppError)
async def handle_app_error(request: Request, error: AppError) -> JSONResponse:
    return error_response(error)


@app.exception_handler(HTTPException)
async def handle_http_error(request: Request, error: HTTPException) -> JSONResponse:
    code = "NOT_FOUND" if error.status_code == 404 else "BAD_FORMAT"
    message = "The requested resource was not found." if error.status_code == 404 else "The request is invalid."
    return error_response(AppError(code, message, http_status=error.status_code))


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
    return error_response(AppError("BAD_FORMAT", "The request is invalid.", http_status=400))


class _UploadParser:
    """Write multipart files directly to the job directory with bounded fields."""

    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir
        self.fields: dict[str, str] = {}
        self.files: list[JobInput] = []
        self.file: BinaryIO | None = None
        self.name = ""
        self.filename: str | None = None
        self.value = bytearray()
        self.headers: dict[bytes, bytes] = {}
        self.header_name = bytearray()
        self.header_value = bytearray()
        self.header_bytes = 0
        self.file_size = 0
        self.parts = 0
        self.complete = False

    def on_part_begin(self) -> None:
        self.parts += 1
        if self.parts > settings.max_items + 6:
            raise AppError("BATCH_TOO_LARGE", "Too many input items.", {"max_items": settings.max_items})
        self.name = ""
        self.filename = None
        self.value.clear()
        self.headers.clear()
        self.header_name.clear()
        self.header_value.clear()
        self.header_bytes = 0
        self.file_size = 0

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self.header_bytes += end - start
        self._check_headers()
        self.header_name.extend(data[start:end])

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self.header_bytes += end - start
        self._check_headers()
        self.header_value.extend(data[start:end])

    def _check_headers(self) -> None:
        if self.header_bytes > 16384:
            raise AppError("BAD_FORMAT", "Multipart headers are too large.")

    def on_header_end(self) -> None:
        key = bytes(self.header_name).lower()
        if key in self.headers:
            raise AppError("BAD_FORMAT", "Duplicate multipart header.")
        self.headers[key] = bytes(self.header_value)
        self.header_name.clear()
        self.header_value.clear()

    def on_headers_finished(self) -> None:
        disposition, options = parse_options_header(self.headers.get(b"content-disposition"))
        if disposition != b"form-data" or b"name" not in options:
            raise AppError("BAD_FORMAT", "Invalid multipart field.")
        self.name = options[b"name"].decode("utf-8")
        if b"filename" in options:
            if self.name != "files":
                raise AppError("BAD_FORMAT", "Unexpected upload field.")
            self.filename = options[b"filename"].decode("utf-8").replace("\\", "/").split("/")[-1]
            if not self.filename:
                raise AppError("UNSUPPORTED_FILE", "The uploaded file has no name.")
            if len(self.files) >= settings.max_items:
                raise AppError("BATCH_TOO_LARGE", "Too many input items.", {"max_items": settings.max_items})
            path = self.workdir / f"upload-{len(self.files)}.bin"
            self.file = path.open("xb")
            self.files.append(JobInput(source=self.filename, upload_path=str(path)))
        elif self.name not in {"urls", "format", "timestamps", "combine", "language"} or self.name in self.fields:
            raise AppError("BAD_FORMAT", "Unexpected or duplicate form field.")

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self.file is not None:
            self.file_size += end - start
            if self.file_size > settings.max_upload_mb * 1024 * 1024:
                raise AppError("TOO_LARGE", "The uploaded file is too large.", {"max_mb": settings.max_upload_mb}, 413)
            self.file.write(data[start:end])
        else:
            if len(self.value) + end - start > 65536:
                raise AppError("BAD_FORMAT", "A form field is too large.")
            self.value.extend(data[start:end])

    def on_part_end(self) -> None:
        if self.file is not None:
            self.file.close()
            self.file = None
        else:
            self.fields[self.name] = self.value.decode("utf-8")

    def on_end(self) -> None:
        self.complete = True

    def close(self) -> None:
        if self.file is not None:
            self.file.close()
            self.file = None


async def _read_form(request: Request, workdir: Path) -> tuple[dict[str, str], list[JobInput]]:
    content_type, options = parse_options_header(request.headers.get("content-type"))
    boundary = options.get(b"boundary", b"")
    if content_type != b"multipart/form-data" or not boundary or len(boundary) > 200:
        raise AppError("BAD_FORMAT", "Expected multipart/form-data.")
    sink = _UploadParser(workdir)
    callbacks = {
        name: getattr(sink, name) for name in (
            "on_part_begin", "on_header_field", "on_header_value", "on_header_end",
            "on_headers_finished", "on_part_data", "on_part_end", "on_end",
        )
    }
    parser = MultipartParser(boundary, callbacks)
    parser.logger = logging.Logger("private-multipart", logging.CRITICAL + 1)
    received = 0
    maximum = settings.max_items * settings.max_upload_mb * 1024 * 1024 + 1024 * 1024
    try:
        async for data in request.stream():
            received += len(data)
            if received > maximum:
                raise AppError("TOO_LARGE", "The upload request is too large.", {"max_mb": settings.max_upload_mb}, 413)
            for offset in range(0, len(data), 65536):
                await _io(parser.write, data[offset:offset + 65536])
        await _io(parser.finalize)
        if not sink.complete:
            raise AppError("BAD_FORMAT", "The multipart body is incomplete.")
        return sink.fields, sink.files
    except AppError:
        raise
    except (ValueError, UnicodeError):
        raise AppError("BAD_FORMAT", "The multipart body is invalid.") from None
    finally:
        await _io(sink.close)


def _boolean(fields: dict[str, str], name: str) -> bool:
    value = fields.get(name, "false").lower()
    if value not in {"true", "false"}:
        raise AppError("BAD_FORMAT", f"The {name} value must be true or false.")
    return value == "true"


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "version": "1"}


@app.get("/api/config")
async def configuration() -> dict[str, Any]:
    return {
        "formats": FORMATS, "combinable": COMBINABLE, "max_items": settings.max_items,
        "max_file_min": settings.max_file_min, "max_upload_mb": settings.max_upload_mb,
        "languages": LANGUAGES,
    }


@app.get("/api/quota")
async def get_quota(request: Request) -> dict[str, Any]:
    return await quota.quota_view(request.state.identity)


@app.post("/api/jobs", status_code=202)
async def create_job(request: Request) -> dict[str, Any]:
    identity = request.state.identity
    manager.assert_available(identity)
    limiter.check_job(get_client_ip(request))
    job_id = secrets.token_urlsafe(16)
    workdir = Path(settings.data_dir) / "tmp" / job_id
    accepted = False
    try:
        await _io(workdir.mkdir, parents=True, exist_ok=False)
        fields, uploads = await _read_form(request, workdir)
        fmt = fields.get("format", "txt").lower()
        if fmt not in FORMATS:
            raise AppError("BAD_FORMAT", "The requested output format is not supported.")
        timestamps, combine = _boolean(fields, "timestamps"), _boolean(fields, "combine")
        language = fields.get("language", "auto").lower()
        if language not in LANGUAGES:
            raise AppError("BAD_FORMAT", "The requested language is not supported.")
        try:
            urls = json.loads(fields.get("urls", "[]"))
        except (json.JSONDecodeError, ValueError):
            raise AppError("URL_INVALID", "URLs must be a JSON array of strings.") from None
        if not isinstance(urls, list) or any(not isinstance(url, str) for url in urls):
            raise AppError("URL_INVALID", "URLs must be a JSON array of strings.")
        if len(urls) + len(uploads) > settings.max_items:
            raise AppError("BATCH_TOO_LARGE", "Too many input items.", {"max_items": settings.max_items})
        if not urls and not uploads:
            raise AppError("NO_INPUT", "Add at least one URL or file.")
        inputs: list[JobInput] = []
        if urls:
            security = import_module("app.security")
            inputs.extend(JobInput(source=security.validate_url(url)) for url in urls)
        inputs.extend(uploads)
        result = manager.submit(job_id, identity, inputs, fmt, timestamps, combine, None if language == "auto" else language)
        accepted = True
        return result
    finally:
        if not accepted:
            await _io(_remove_workdir, workdir)


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str, request: Request) -> dict[str, Any]:
    return await manager.snapshot(job_id, request.state.identity)


@app.get("/api/jobs/{job_id}/download")
async def download_job(job_id: str, request: Request) -> FileResponse:
    path, filename, mime = await manager.download(job_id, request.state.identity)
    safe_name = filename.replace("\r", "").replace("\n", "")
    fallback = re.sub(r"[^A-Za-z0-9._ -]", "_", safe_name).strip(" .") or "transcript"
    disposition = f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(safe_name, safe='')}"
    return FileResponse(path, media_type=mime, headers={"Content-Disposition": disposition})


@app.delete("/api/jobs/{job_id}")
async def cancel_job(job_id: str, request: Request) -> dict[str, bool]:
    await manager.cancel(job_id, request.state.identity)
    return {"ok": True}


static_dir = Path(__file__).resolve().parent.parent / "static"
if static_dir.is_dir():
    app.mount("/", SitePages(directory=static_dir, html=True), name="static")
