from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal, Optional


class AppError(Exception):
    """User-facing error. `message` is English; the frontend localizes by `code`."""

    def __init__(self, code: str, message: str, params: Optional[dict] = None, http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.params = params or {}
        self.http_status = http_status


@dataclass
class Segment:
    start: float          # seconds from the start of the FULL audio
    end: float
    text: str


@dataclass
class AudioInfo:
    path: str             # local path of the downloaded/uploaded original
    duration_s: float
    title: str            # video title or file name (without extension)
    source: str           # original URL or uploaded file name


@dataclass
class Chunk:
    path: str             # mono 16 kHz MP3 (64 kbps) of this chunk
    offset_s: float       # where the chunk starts in the full audio
    duration_s: float


@dataclass
class RemoteProbe:
    title: str
    duration_s: Optional[float]      # None when unknown before download
    kind: Literal["ytdlp", "direct"]


@dataclass
class Transcript:
    title: str
    source: str
    language: str                    # ISO 639-1, e.g. "en"
    duration_s: float
    segments: list[Segment] = field(default_factory=list)


@dataclass
class RenderedFile:
    filename: str
    mime: str
    data: bytes
