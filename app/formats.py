"""Output renderers: TXT, MD, DOCX, SRT and VTT, plus a combined document and a ZIP of files.

All functions are synchronous and CPU-bound (DOCX takes a few hundred ms for long transcripts):
call them with asyncio.to_thread from async code.
"""
from __future__ import annotations

import io
import math
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone

from docx import Document
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

from app.models import AppError, RenderedFile, Transcript

FORMATS = ("txt", "md", "docx", "srt", "vtt")
COMBINABLE = ("txt", "md", "docx")

_MIME = {
    "txt": "text/plain",
    "md": "text/markdown",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "srt": "application/x-subrip",
    "vtt": "text/vtt",
}
ZIP_MIME = "application/zip"

PARAGRAPH_SEGMENTS = 5   # segments per paragraph when timestamps are off
PARAGRAPH_PAUSE_S = 2.0  # a longer pause between segments starts a new paragraph
CUE_MAX_S = 7.0          # longer subtitles are split ...
CUE_MAX_CHARS = 84       # ... and so are those with more characters (2 lines x 42)
LINE_CHARS = 42
MIN_PIECE_CHARS = 15     # a long but sparse segment is never cut into pieces shorter than this
DEGENERATE_CUE_S = 0.5   # display time for a cue whose end is not after its start
FILENAME_MAX = 80        # characters, extension included
FILENAME_MAX_BYTES = 200  # UTF-8 bytes of the name stem (filesystems allow 255)

# ---------------------------------------------------------------- text helpers

_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff\ufffe\uffff]")
_NO_SPACE = re.compile("[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")


def _clean(text: str) -> str:
    """One line of text: whitespace collapsed; control characters and lone surrogates removed (XML-safe)."""
    return " ".join(_ILLEGAL.sub("", " ".join((text or "").split())).split())


def _finite(value: float) -> float:
    """A non-negative finite float (NaN, infinity and negatives become 0)."""
    number = float(value)
    return number if math.isfinite(number) and number > 0 else 0.0


def _title(t: Transcript) -> str:
    return _clean(t.title) or "Transcript"


def _join(a: str, b: str) -> str:
    """Join two text pieces with a space, except next to Chinese/Japanese characters."""
    return a + b if _NO_SPACE.match(a[-1]) or _NO_SPACE.match(b[0]) else f"{a} {b}"


@dataclass(frozen=True)
class _Seg:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class _Block:
    mark: str  # "[mm:ss]" label without brackets; "" when timestamps are off
    text: str


def _segments(t: Transcript) -> list[_Seg]:
    out: list[_Seg] = []
    for seg in t.segments:
        text = _clean(seg.text)
        if text:
            start = _finite(seg.start)
            out.append(_Seg(start, max(_finite(seg.end), start), text))
    return out


def _stamp(seconds: float, hours: bool) -> str:
    """mm:ss, or h:mm:ss when `hours` (decided once per document so the width stays consistent)."""
    total = int(_finite(seconds))
    if hours:
        return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"
    return f"{total // 60:02d}:{total % 60:02d}"


def _clock(seconds: float) -> str:
    total = round(_finite(seconds))
    return _stamp(total, total >= 3600)


def _paragraphs(segs: list[_Seg]) -> list[str]:
    """Running text: a new paragraph every ~5 segments or after a pause longer than 2 s."""
    paragraphs: list[str] = []
    current, count, prev_end = "", 0, 0.0
    for seg in segs:
        if current and (count >= PARAGRAPH_SEGMENTS or seg.start - prev_end > PARAGRAPH_PAUSE_S):
            paragraphs.append(current)
            current, count = "", 0
        current = _join(current, seg.text) if current else seg.text
        count += 1
        prev_end = seg.end
    if current:
        paragraphs.append(current)
    return paragraphs


def _blocks(t: Transcript, timestamps: bool) -> list[_Block]:
    segs = _segments(t)
    if not timestamps:
        return [_Block("", text) for text in _paragraphs(segs)]
    hours = max([_finite(t.duration_s)] + [s.start for s in segs]) >= 3600
    return [_Block(_stamp(s.start, hours), s.text) for s in segs]


def _meta(t: Transcript, md: bool) -> str:
    """'Source: ... · Duration: ... · Language: ...' (labels are English: render() has no locale)."""
    parts: list[str] = []
    source = _clean(t.source)
    if source:
        if md and re.fullmatch(r"https?://[^\s<>]+", source):
            source = f"<{source}>"  # autolink: no escaping inside
        elif md:
            source = _md_escape(source)
        parts.append(f"Source: {source}")
    parts.append(f"Duration: {_clock(t.duration_s)}")
    lang = _clean(t.language).lower()
    parts.append(f"Language: {lang if lang and lang != 'und' else 'unknown'}")
    return " · ".join(parts)


_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>~])")


def _md_escape(text: str, block_start: bool = False) -> str:
    """Escape Markdown syntax (and raw HTML) in plain text; `block_start` also guards list/heading markers."""
    out = _MD_SPECIAL.sub(r"\\\1", text)
    if block_start:
        if re.match(r"(#{1,6}|[+-])(\s|$)", out):
            out = "\\" + out
        else:
            out = re.sub(r"^(\d+)([.)])(\s)", r"\1\\\2\3", out)
    return out


# ---------------------------------------------------------------- TXT / MD / DOCX

def _txt_body(t: Transcript, timestamps: bool) -> str:
    blocks = _blocks(t, timestamps)
    if timestamps:
        return "\n".join(f"[{b.mark}] {b.text}" for b in blocks)
    return "\n\n".join(b.text for b in blocks)


def _txt_single(t: Transcript, timestamps: bool) -> bytes:
    body = _txt_body(t, timestamps)
    return (body + "\n" if body else "").encode("utf-8")


def _txt_combined(ts: list[Transcript], timestamps: bool) -> bytes:
    sections = []
    for t in ts:
        title = _title(t)
        sections.append(f"{title}\n{'=' * min(len(title), 72)}\n\n{_txt_body(t, timestamps)}".rstrip("\n"))
    return ("\n\n\n".join(sections) + "\n").encode("utf-8")


def _md_section(t: Transcript, timestamps: bool) -> str:
    head = f"# {_md_escape(_title(t))}\n\n{_meta(t, md=True)}"
    blocks = _blocks(t, timestamps)
    if timestamps:  # one paragraph per segment so each stays on its own line when rendered
        body = "\n\n".join(f"**[{b.mark}]** {_md_escape(b.text)}" for b in blocks)
    else:
        body = "\n\n".join(_md_escape(b.text, block_start=True) for b in blocks)
    return f"{head}\n\n{body}" if body else head


def _md(ts: list[Transcript], timestamps: bool) -> bytes:
    return ("\n\n---\n\n".join(_md_section(t, timestamps) for t in ts) + "\n").encode("utf-8")


def _docx(ts: list[Transcript], timestamps: bool, doc_title: str) -> bytes:
    doc = Document()
    zoom = doc.settings.element.find(qn("w:zoom"))
    if zoom is not None:  # the stock template omits the attribute the schema requires
        zoom.set(qn("w:percent"), "100")
    props = doc.core_properties
    props.title, props.author, props.last_modified_by, props.comments = doc_title[:255], "", "", ""
    props.created = props.modified = datetime.now(timezone.utc)
    for i, t in enumerate(ts):
        heading = doc.add_heading(_title(t), level=1)
        if i:
            heading.paragraph_format.page_break_before = True
        meta = doc.add_paragraph().add_run(_meta(t, md=False))
        meta.italic = True
        meta.font.size = Pt(9)
        meta.font.color.rgb = RGBColor(0x59, 0x59, 0x59)
        for block in _blocks(t, timestamps):
            para = doc.add_paragraph()
            para.paragraph_format.space_after = Pt(4 if block.mark else 8)
            if block.mark:
                para.add_run(f"[{block.mark}]").bold = True
                para.add_run(f" {block.text}")
            else:
                para.add_run(block.text)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- SRT / VTT

@dataclass(frozen=True)
class _Cue:
    start: float
    end: float
    lines: tuple[str, ...]


_BREAK_BONUS = {
    ".": 6, "?": 6, "!": 6, "…": 6, "。": 6, "！": 6, "？": 6,
    ",": 3, ";": 3, ":": 3, "，": 3, "、": 3, "；": 3, "：": 3,
}
_CJK_BREAKS = "。！？…，、；："


def _cut_near(text: str, target: int) -> int:
    """Index near `target` where `text` is best cut: at a space (dropped) or after CJK punctuation.

    Sentence and clause punctuation are preferred; without any candidate it cuts hard at `target`.
    """
    last = len(text) - 1
    target = min(max(target, 1), last)
    window = max(10, len(text) // 4)
    best, best_score = target, math.inf
    for i in range(max(1, target - window), min(last, target + window) + 1):
        if text[i] == " " or text[i - 1] in _CJK_BREAKS:
            score = abs(i - target) - _BREAK_BONUS.get(text[i - 1], 0)
            if score < best_score:
                best, best_score = i, score
    return best


def _split(text: str, n: int) -> list[str]:
    """Split text into n balanced pieces."""
    if n <= 1 or len(text) < 2:
        return [text]
    left = n // 2
    i = _cut_near(text, round(len(text) * left / n))
    return _split(text[:i].rstrip(), left) + _split(text[i:].lstrip(), n - left)


def _wrap2(text: str) -> list[str]:
    """At most two balanced lines of ~42 characters."""
    if len(text) <= LINE_CHARS:
        return [text]
    i = _cut_near(text, len(text) // 2)
    return [text[:i].rstrip(), text[i:].lstrip()]


def _cues(t: Transcript) -> list[_Cue]:
    """Subtitles: segments over ~7 s or ~84 characters are split, time shared in proportion to text length."""
    cues: list[_Cue] = []
    for seg in _segments(t):
        text = seg.text.replace("-->", "→")
        end = seg.end if seg.end > seg.start else seg.start + DEGENERATE_CUE_S
        duration = end - seg.start
        by_chars = math.ceil(len(text) / CUE_MAX_CHARS)
        by_time = math.ceil(duration / CUE_MAX_S - 1e-6)
        pieces = [p for p in _split(text, max(by_chars, min(by_time, len(text) // MIN_PIECE_CHARS))) if p]
        total = sum(len(p) for p in pieces)
        done = 0
        for piece in pieces:
            a = seg.start + duration * done / total
            done += len(piece)
            b = seg.start + duration * done / total
            cues.append(_Cue(a, b, tuple(_wrap2(piece))))
    return cues


def _hms(seconds: float, sep: str) -> str:
    hours, rest = divmod(round(_finite(seconds) * 1000), 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{sep}{millis:03d}"


def _srt(t: Transcript) -> bytes:
    blocks = [
        f"{n}\n{_hms(c.start, ',')} --> {_hms(c.end, ',')}\n" + "\n".join(c.lines)
        for n, c in enumerate(_cues(t), 1)
    ]
    return ("\n\n".join(blocks) + "\n").encode("utf-8") if blocks else b""


def _vtt_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _vtt(t: Transcript) -> bytes:
    blocks = [
        f"{_hms(c.start, '.')} --> {_hms(c.end, '.')}\n" + "\n".join(_vtt_escape(line) for line in c.lines)
        for c in _cues(t)
    ]
    return ("WEBVTT\n\n" + "\n\n".join(blocks) + "\n").encode("utf-8") if blocks else b"WEBVTT\n"


# ---------------------------------------------------------------- file names

_NAME_FORBIDDEN = re.compile(r'[<>:"|?*]')
_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
)


def safe_filename(title: str, ext: str) -> str:
    """File name that is safe on Windows, macOS and Linux (accented letters are kept).

    Removes control and bidi characters and `<>:"|?*`, turns `/` and `\\` into `-`, avoids reserved
    Windows names, caps the whole name at 80 characters and falls back to `transcript`.
    """
    ext = re.sub(r"[^a-z0-9]", "", ext.lower()) or "txt"
    name = " ".join(unicodedata.normalize("NFC", title or "").split())
    name = "".join(ch for ch in name if unicodedata.category(ch) not in ("Cc", "Cf", "Cs"))
    name = _NAME_FORBIDDEN.sub("", name.replace("/", "-").replace("\\", "-"))
    stem = " ".join(name.split()).strip(" .-")
    if stem.split(".")[0].lower() in _RESERVED_NAMES:
        stem = f"_{stem}"
    stem = stem[: FILENAME_MAX - len(ext) - 1].rstrip(" .-")
    while len(stem.encode("utf-8")) > FILENAME_MAX_BYTES:
        stem = stem[:-1].rstrip(" .-")
    return f"{stem or 'transcript'}.{ext}"


def _unique_names(names: list[str]) -> list[str]:
    """`title.txt`, `title (2).txt`, ... compared case-insensitively (Windows and macOS)."""
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        stem, dot, ext = name.rpartition(".")
        candidate, n = name, 1
        while candidate.lower() in seen:
            n += 1
            candidate = f"{stem} ({n}){dot}{ext}"
        seen.add(candidate.lower())
        out.append(candidate)
    return out


# ---------------------------------------------------------------- public API

def _require(fmt: object, allowed: tuple[str, ...], message: str) -> None:
    if not isinstance(fmt, str) or fmt not in allowed:
        raise AppError("BAD_FORMAT", message, http_status=400)


def render(t: Transcript, fmt: str, timestamps: bool) -> RenderedFile:
    """One transcript in one format. SRT and VTT always carry times (`timestamps` is ignored)."""
    _require(fmt, FORMATS, "Unsupported output format.")
    if fmt == "txt":
        data = _txt_single(t, timestamps)
    elif fmt == "md":
        data = _md([t], timestamps)
    elif fmt == "docx":
        data = _docx([t], timestamps, _title(t))
    elif fmt == "srt":
        data = _srt(t)
    else:
        data = _vtt(t)
    return RenderedFile(filename=safe_filename(t.title, fmt), mime=_MIME[fmt], data=data)


def render_combined(ts: list[Transcript], fmt: str, timestamps: bool) -> RenderedFile:
    """One document with a section per transcript (txt, md and docx only): transcripts.<ext>."""
    _require(fmt, COMBINABLE, "This format cannot be combined into a single file.")
    if not ts:
        raise AppError("NO_INPUT", "There are no transcripts to combine.")
    if fmt == "txt":
        data = _txt_combined(ts, timestamps)
    elif fmt == "md":
        data = _md(ts, timestamps)
    else:
        data = _docx(ts, timestamps, "Transcripts")
    return RenderedFile(filename=f"transcripts.{fmt}", mime=_MIME[fmt], data=data)


def render_zip(ts: list[Transcript], fmt: str, timestamps: bool) -> RenderedFile:
    """One file per transcript inside transcripts.zip; repeated titles get ` (2)`, ` (3)`, ..."""
    _require(fmt, FORMATS, "Unsupported output format.")
    if not ts:
        raise AppError("NO_INPUT", "There are no transcripts to archive.")
    files = [render(t, fmt, timestamps) for t in ts]
    names = _unique_names([f.filename for f in files])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, rendered in zip(names, files):
            archive.writestr(name, rendered.data)
    return RenderedFile(filename="transcripts.zip", mime=ZIP_MIME, data=buf.getvalue())
