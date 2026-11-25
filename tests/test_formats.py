"""Tests for app.formats."""
from __future__ import annotations

import io
import random
import re
import zipfile

import pytest
from docx import Document

from app.formats import COMBINABLE, FORMATS, render, render_combined, render_zip, safe_filename
from app.models import AppError, Segment, Transcript

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def seg(start: float, end: float, text: str) -> Segment:
    return Segment(start, end, text)


def make(segments, *, title="My Talk", source="https://youtu.be/abc", language="en", duration=95.0) -> Transcript:
    return Transcript(title=title, source=source, language=language, duration_s=duration, segments=segments)


BASIC = [seg(0.0, 3.2, "Hello there."), seg(3.2, 7.0, "General Kenobi."), seg(65.9, 70.0, "Later.")]


def text(rendered) -> str:
    return rendered.data.decode("utf-8")


def docx_paragraphs(rendered) -> list[str]:
    return [p.text for p in Document(io.BytesIO(rendered.data)).paragraphs]


def to_seconds(stamp: str) -> float:
    hours, minutes, seconds = stamp.replace(",", ".").split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def parse_cues(raw: str, vtt: bool = False):
    """[(start, end, [lines])] from SRT or VTT text."""
    blocks = raw.strip().split("\n\n")
    if vtt:
        assert blocks.pop(0) == "WEBVTT"
    cues = []
    for block in blocks:
        lines = block.split("\n")
        if not vtt:
            lines = lines[1:]  # drop the index
        start, end = (to_seconds(x) for x in lines[0].split(" --> "))
        cues.append((start, end, lines[1:]))
    return cues


# ------------------------------------------------------------------ TXT

def test_txt_with_timestamps():
    rf = render(make(BASIC), "txt", True)
    assert text(rf) == "[00:00] Hello there.\n[00:03] General Kenobi.\n[01:05] Later.\n"
    assert (rf.filename, rf.mime) == ("My Talk.txt", "text/plain")
    assert b"\r" not in rf.data


def test_txt_hours_label_is_used_everywhere_for_long_audio():
    t = make([seg(5, 9, "Start."), seg(65, 70, "Middle."), seg(3605, 3610, "End.")], duration=3725.0)
    assert text(render(t, "txt", True)) == "[0:00:05] Start.\n[0:01:05] Middle.\n[1:00:05] End.\n"


def test_txt_hours_label_when_a_segment_starts_after_an_hour_despite_short_duration():
    t = make([seg(10, 12, "a"), seg(3601, 3602, "b")], duration=3599.0)
    assert text(render(t, "txt", True)) == "[0:00:10] a\n[1:00:01] b\n"


def test_txt_just_under_an_hour_keeps_mm_ss():
    t = make([seg(3599.2, 3599.5, "end")], duration=3599.5)
    assert text(render(t, "txt", True)) == "[59:59] end\n"


def test_txt_without_timestamps_groups_five_segments_per_paragraph():
    segs = [seg(i, i + 1, f"S{i}.") for i in range(12)]
    assert text(render(make(segs), "txt", False)) == "S0. S1. S2. S3. S4.\n\nS5. S6. S7. S8. S9.\n\nS10. S11.\n"


def test_txt_without_timestamps_breaks_on_pause_longer_than_two_seconds():
    segs = [seg(0, 1, "A."), seg(3, 4, "B."), seg(6.5, 7, "C.")]  # gaps: exactly 2 s (same paragraph), 2.5 s (new)
    assert text(render(make(segs), "txt", False)) == "A. B.\n\nC.\n"


def test_txt_chinese_and_japanese_are_joined_without_spaces():
    assert text(render(make([seg(0, 1, "你好。"), seg(1, 2, "世界。")]), "txt", False)) == "你好。世界。\n"
    assert text(render(make([seg(0, 1, "Hello."), seg(1, 2, "World.")]), "txt", False)) == "Hello. World.\n"


def test_special_characters_and_emojis_survive():
    raw = "¿Qué tal? 😀 café — “quotes” & <b>bold</b> 日本語 👨‍👩‍👧"
    t = make([seg(0, 2, raw)])
    assert raw in text(render(t, "txt", True))
    srt_lines = parse_cues(text(render(t, "srt", True)))[0][2]
    assert len(srt_lines) == 2 and " ".join(srt_lines) == raw  # a long line is wrapped in two
    vtt_lines = parse_cues(text(render(t, "vtt", True)), vtt=True)[0][2]
    assert " ".join(vtt_lines) == "¿Qué tal? 😀 café — “quotes” &amp; &lt;b&gt;bold&lt;/b&gt; 日本語 👨‍👩‍👧"
    assert "¿Qué tal? 😀 café — “quotes” & \\<b\\>bold\\</b\\> 日本語 👨‍👩‍👧" in text(render(t, "md", True))
    assert f"[00:00] {raw}" in docx_paragraphs(render(t, "docx", True))


def test_control_characters_and_lone_surrogates_are_removed():
    t = make([seg(0, 1, "bad\x00char\x07 and\ttab\u2028line\r\nnext"), seg(1, 2, "lone \ud83d surrogate")])
    assert text(render(t, "txt", True)) == "[00:00] badchar and tab line next\n[00:01] lone surrogate\n"
    assert docx_paragraphs(render(t, "docx", True))[-1] == "[00:01] lone surrogate"  # python-docx rejects control chars


def test_empty_segments_are_skipped_and_empty_transcript_renders():
    t = make([seg(0, 1, "   "), seg(1, 2, "Real."), seg(2, 3, "")])
    assert text(render(t, "txt", True)) == "[00:01] Real.\n"
    assert text(render(t, "srt", True)).startswith("1\n00:00:01,000 --> 00:00:02,000\nReal.")
    empty = make([])
    assert text(render(empty, "txt", True)) == ""
    assert text(render(empty, "srt", True)) == ""
    assert text(render(empty, "vtt", True)) == "WEBVTT\n"
    assert text(render(empty, "md", True)).startswith("# My Talk")
    assert docx_paragraphs(render(empty, "docx", True))[0] == "My Talk"


# ------------------------------------------------------------------ MD

def test_md_with_timestamps_has_title_metadata_and_bold_marks():
    rf = render(make(BASIC), "md", True)
    assert text(rf) == (
        "# My Talk\n\n"
        "Source: <https://youtu.be/abc> · Duration: 01:35 · Language: en\n\n"
        "**[00:00]** Hello there.\n\n**[00:03]** General Kenobi.\n\n**[01:05]** Later.\n"
    )
    assert (rf.filename, rf.mime) == ("My Talk.md", "text/markdown")


def test_md_without_timestamps_uses_paragraphs():
    segs = [seg(i, i + 1, f"S{i}.") for i in range(6)]
    body = text(render(make(segs), "md", False)).split("\n\n", 2)[2]
    assert body == "S0. S1. S2. S3. S4.\n\nS5.\n"


def test_md_escapes_markdown_and_html():
    t = make([seg(0, 1, "2 * 3 and snake_case <b> [link] `code`")], title="C# <Intro> *wow*", source="my talk_1.mp3",
             language="und", duration=3723.0)
    md = text(render(t, "md", True))
    assert md.startswith("# C# \\<Intro\\> \\*wow\\*\n\nSource: my talk\\_1.mp3 · Duration: 1:02:03 · Language: unknown\n")
    assert "**[0:00:00]** 2 \\* 3 and snake\\_case \\<b\\> \\[link\\] \\`code\\`" in md


def test_md_without_timestamps_guards_block_markers():
    t = make([seg(0, 1, "- not a bullet"), seg(5, 6, "1. not a list"), seg(10, 11, "# not a heading")])
    body = text(render(t, "md", False)).split("\n\n", 2)[2]
    assert body == "\\- not a bullet\n\n1\\. not a list\n\n\\# not a heading\n"


# ------------------------------------------------------------------ DOCX

def test_docx_reopens_with_python_docx():
    rf = render(make(BASIC), "docx", True)
    assert (rf.filename, rf.mime) == ("My Talk.docx", DOCX_MIME)
    assert rf.data[:2] == b"PK"
    doc = Document(io.BytesIO(rf.data))
    assert [p.text for p in doc.paragraphs if p.style.name == "Heading 1"] == ["My Talk"]
    assert [p.text for p in doc.paragraphs] == [
        "My Talk",
        "Source: https://youtu.be/abc · Duration: 01:35 · Language: en",
        "[00:00] Hello there.",
        "[00:03] General Kenobi.",
        "[01:05] Later.",
    ]
    assert doc.paragraphs[2].runs[0].bold is True
    assert doc.core_properties.title == "My Talk"
    assert doc.core_properties.author == ""


def test_docx_without_timestamps_uses_paragraphs():
    segs = [seg(i, i + 1, f"S{i}.") for i in range(6)]
    assert docx_paragraphs(render(make(segs), "docx", False))[2:] == ["S0. S1. S2. S3. S4.", "S5."]


# ------------------------------------------------------------------ SRT / VTT

def test_srt_basic_and_timestamps_flag_is_ignored():
    t = make([seg(0, 2.5, "Hello."), seg(2.5, 5, "World.")])
    expected = "1\n00:00:00,000 --> 00:00:02,500\nHello.\n\n2\n00:00:02,500 --> 00:00:05,000\nWorld.\n"
    rf = render(t, "srt", False)
    assert text(rf) == expected == text(render(t, "srt", True))
    assert (rf.filename, rf.mime) == ("My Talk.srt", "application/x-subrip")


def test_srt_hours_and_milliseconds():
    t = make([seg(3725.5, 3727.04, "Late.")], duration=4000.0)
    assert "1\n01:02:05,500 --> 01:02:07,040\nLate." in text(render(t, "srt", True))


def test_srt_splits_long_segments_into_short_two_line_cues():
    long_text = " ".join(("lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 4).split())
    cues = parse_cues(text(render(make([seg(10.0, 30.0, long_text)], duration=40.0), "srt", True)))
    assert len(cues) >= 3
    assert cues[0][0] == pytest.approx(10.0, abs=0.001) and cues[-1][1] == pytest.approx(30.0, abs=0.001)
    for (_, end, _), (start, _, _) in zip(cues, cues[1:]):
        assert end == pytest.approx(start, abs=0.002)  # contiguous
    for start, end, lines in cues:
        assert end - start <= 7.01
        assert 1 <= len(lines) <= 2 and all(0 < len(line) <= 48 for line in lines)
    assert " ".join(" ".join(lines) for _, _, lines in cues) == long_text


def test_srt_numbers_cues_sequentially_after_splitting():
    long_text = " ".join(["word"] * 60)  # 299 chars in 5 s
    raw = text(render(make([seg(0, 5, long_text), seg(5, 6, "Short.")]), "srt", True))
    indexes = [int(block.split("\n")[0]) for block in raw.strip().split("\n\n")]
    assert indexes == list(range(1, len(indexes) + 1)) and len(indexes) >= 4


def test_srt_splits_by_duration_only_when_the_text_is_long_enough():
    sparse = make([seg(0, 20, "Thank you.")])
    assert len(parse_cues(text(render(sparse, "srt", True)))) == 1  # 10 chars: not worth cutting
    slow = make([seg(0, 15, "one two three four five six seven eight nine ten eleven twelve")])
    cues = parse_cues(text(render(slow, "srt", True)))
    assert len(cues) == 3 and all(end - start <= 7.01 for start, end, _ in cues)


def test_srt_splits_text_without_spaces_after_punctuation():
    zh = "这是一个没有空格的很长的句子。" * 8  # 120 characters
    cues = parse_cues(text(render(make([seg(0, 6, zh)], language="zh"), "srt", True)))
    assert len(cues) == 2
    assert "".join("".join(lines) for _, _, lines in cues) == zh
    assert all(len(lines) <= 2 for _, _, lines in cues)


def test_srt_hard_cuts_an_unbroken_token():
    blob = "x" * 200
    cues = parse_cues(text(render(make([seg(0, 4, blob)]), "srt", True)))
    assert "".join("".join(lines) for _, _, lines in cues) == blob
    assert all(len(lines) <= 2 for _, _, lines in cues)


def test_srt_zero_length_segment_gets_a_visible_duration_and_arrow_is_neutralised():
    raw = text(render(make([seg(5, 5, "Hi --> there")]), "srt", True))
    assert raw == "1\n00:00:05,000 --> 00:00:05,500\nHi → there\n"


def test_vtt_header_dot_separator_and_escaping():
    t = make([seg(0, 2.5, "Fish & chips <3"), seg(2.5, 5, "World.")])
    rf = render(t, "vtt", False)
    assert text(rf) == (
        "WEBVTT\n\n00:00:00.000 --> 00:00:02.500\nFish &amp; chips &lt;3\n\n00:00:02.500 --> 00:00:05.000\nWorld.\n"
    )
    assert (rf.filename, rf.mime) == ("My Talk.vtt", "text/vtt")
    assert text(rf) == text(render(t, "vtt", True))


def test_vtt_uses_the_same_splitting_rules():
    long_text = " ".join(("lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 4).split())
    raw = text(render(make([seg(10.0, 30.0, long_text)], duration=40.0), "vtt", True))
    assert "," not in raw.split("\n\n")[1].split("\n")[0]
    cues = parse_cues(raw, vtt=True)
    assert len(cues) >= 3 and all(1 <= len(lines) <= 2 for _, _, lines in cues)
    assert cues[-1][1] == pytest.approx(30.0, abs=0.001)


def test_arbitrary_text_never_breaks_any_format():
    """Deterministic fuzz: controls, lone surrogates, markup, emoji and CJK must not raise anywhere."""
    rng = random.Random(1234)
    pool = list("abc xyz  .,;:!?-*_#<>&[]`~\\|\t\n") + ["\x00", "\x07", "\x1f", "\x85", "\ud83d", "\ufffe", "\u2028",
            "\u200d", "😀", "é", "日", "。", "，", "--> ", "&amp;"]
    for _ in range(40):
        segs = []
        for i in range(rng.randint(0, 6)):
            start = rng.choice([0.0, i * 3.0, rng.uniform(0, 4000), float("nan")])
            words = "".join(rng.choice(pool) for _ in range(rng.randint(0, 160)))
            segs.append(seg(start, start + rng.choice([0.0, 0.4, 5.0, 31.0]), words))
        t = make(segs, title="".join(rng.choice(pool) for _ in range(30)), source="".join(rng.choice(pool) for _ in range(20)))
        for fmt in FORMATS:
            out = render(t, fmt, bool(rng.getrandbits(1)))
            if fmt != "docx":
                out.data.decode("utf-8")  # text formats are always valid UTF-8
            assert re.fullmatch(r"[^<>:\"/\\|?*\x00-\x1f\x7f]+\.[a-z0-9]+", out.filename)
        Document(io.BytesIO(render(t, "docx", True).data))
        parse_cues(text(render(t, "vtt", True)), vtt=True)


# ------------------------------------------------------------------ file names

@pytest.mark.parametrize("title, expected", [
    ("Intro to Python", "Intro to Python.txt"),
    ("Café: ¿Qué es? <ñandú>", "Café ¿Qué es ñandú.txt"),
    ("Cafe\u0301", "Caf\u00e9.txt"),
    ("a/b\\c", "a-b-c.txt"),
    ("../../etc/passwd", "etc-passwd.txt"),
    ("evil\u202egnp.exe", "evilgnp.exe.txt"),
    ("   ...   ", "transcript.txt"),
    ("", "transcript.txt"),
    ("CON", "_CON.txt"),
    ("nul.backup", "_nul.backup.txt"),
    ("tab\tand\nnewline", "tab and newline.txt"),
])
def test_safe_filename_examples(title, expected):
    assert safe_filename(title, "txt") == expected


def test_safe_filename_limits_length_and_bytes():
    name = safe_filename("x" * 200, "docx")
    assert len(name) == 80 and name.endswith(".docx")
    assert len(safe_filename("ñ" * 200, "txt")) <= 80
    emoji = safe_filename("😀" * 200, "txt")
    assert len(emoji.removesuffix(".txt").encode("utf-8")) <= 200


def test_safe_filename_has_no_dangerous_characters():
    name = safe_filename('<>:"/\\|?*\x00\x1f\x7f..  a  ..', "TXT.")
    assert not re.search(r'[<>:"/\\|?*\x00-\x1f\x7f]', name)
    assert name.endswith(".txt") and not name.startswith((".", " ", "-"))


def test_render_uses_the_safe_filename():
    rf = render(make(BASIC, title='Q&A: "Why?" / Part 2'), "md", True)
    assert rf.filename == "Q&A Why - Part 2.md"


# ------------------------------------------------------------------ errors

@pytest.mark.parametrize("fmt", ["pdf", "TXT", "", "txt ", None])
def test_render_rejects_unknown_formats(fmt):
    with pytest.raises(AppError) as exc:
        render(make(BASIC), fmt, True)
    assert exc.value.code == "BAD_FORMAT" and exc.value.http_status == 400


def test_format_constants():
    assert FORMATS == ("txt", "md", "docx", "srt", "vtt")
    assert COMBINABLE == ("txt", "md", "docx")


# ------------------------------------------------------------------ combined

def sample_set() -> list[Transcript]:
    return [make(BASIC, title="First"), make([seg(0, 2, "Second doc.")], title="Second", source="clip.mp3", duration=2.0)]


def test_combined_txt():
    rf = render_combined(sample_set(), "txt", True)
    assert (rf.filename, rf.mime) == ("transcripts.txt", "text/plain")
    assert text(rf) == (
        "First\n=====\n\n[00:00] Hello there.\n[00:03] General Kenobi.\n[01:05] Later.\n\n\n"
        "Second\n======\n\n[00:00] Second doc.\n"
    )


def test_combined_md_has_one_section_per_transcript():
    rf = render_combined(sample_set(), "md", False)
    raw = text(rf)
    assert (rf.filename, rf.mime) == ("transcripts.md", "text/markdown")
    assert re.findall(r"^# .*$", raw, re.M) == ["# First", "# Second"]
    assert "\n\n---\n\n" in raw and "**[" not in raw


def test_combined_docx_has_heading_and_page_break_per_transcript():
    rf = render_combined(sample_set(), "docx", True)
    assert (rf.filename, rf.mime) == ("transcripts.docx", DOCX_MIME)
    doc = Document(io.BytesIO(rf.data))
    headings = [p for p in doc.paragraphs if p.style.name == "Heading 1"]
    assert [h.text for h in headings] == ["First", "Second"]
    assert not headings[0].paragraph_format.page_break_before
    assert headings[1].paragraph_format.page_break_before is True
    assert "[00:00] Second doc." in [p.text for p in doc.paragraphs]
    assert doc.core_properties.title == "Transcripts"


@pytest.mark.parametrize("fmt", ["srt", "vtt", "pdf", "TXT", None])
def test_combined_only_accepts_combinable_formats(fmt):
    with pytest.raises(AppError) as exc:
        render_combined(sample_set(), fmt, True)
    assert exc.value.code == "BAD_FORMAT"


def test_combined_and_zip_need_at_least_one_transcript():
    with pytest.raises(AppError) as exc:
        render_combined([], "txt", True)
    assert exc.value.code == "NO_INPUT"
    with pytest.raises(AppError) as exc:
        render_zip([], "txt", True)
    assert exc.value.code == "NO_INPUT"


# ------------------------------------------------------------------ zip

def test_zip_one_file_per_transcript_with_unique_names():
    ts = [make(BASIC, title="Same"), make(BASIC, title="Same"), make(BASIC, title="Other"), make(BASIC, title="same")]
    rf = render_zip(ts, "txt", True)
    assert (rf.filename, rf.mime) == ("transcripts.zip", "application/zip")
    with zipfile.ZipFile(io.BytesIO(rf.data)) as archive:
        assert archive.testzip() is None
        assert archive.namelist() == ["Same.txt", "Same (2).txt", "Other.txt", "same (3).txt"]
        assert archive.read("Other.txt") == render(ts[2], "txt", True).data
        assert all(info.compress_type == zipfile.ZIP_DEFLATED for info in archive.infolist())


def test_zip_accepts_every_format_and_keeps_accents():
    ts = [make(BASIC, title="Canción — ñandú"), make([seg(0, 1, "Hi.")], title="Other")]
    for fmt in FORMATS:
        rf = render_zip(ts, fmt, False)
        with zipfile.ZipFile(io.BytesIO(rf.data)) as archive:
            assert archive.namelist() == [f"Canción — ñandú.{fmt}", f"Other.{fmt}"]
            assert archive.read(f"Other.{fmt}") == render(ts[1], fmt, False).data
    with zipfile.ZipFile(io.BytesIO(render_zip(ts, "docx", True).data)) as archive:
        inner = archive.read("Other.docx")
    assert Document(io.BytesIO(inner)).paragraphs[0].text == "Other"


def test_zip_rejects_bad_format():
    with pytest.raises(AppError) as exc:
        render_zip(sample_set(), "pdf", True)
    assert exc.value.code == "BAD_FORMAT"
