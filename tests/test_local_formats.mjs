import assert from "node:assert/strict";
import test from "node:test";
import { makeLocalResult } from "../static/assets/local-formats.mjs";

const sample = {
  source: "entrevista.mp3",
  format: "txt",
  timestamps: false,
  text: "  Hola, ¿qué tal? Todo bien.  ",
  duration: 3.5,
  segments: [
    { start: 0.25, end: 1.2, text: " Hola, ¿qué tal? " },
    { start: 1.5, end: 3, text: " Todo bien. " },
  ],
};

test("TXT preserves the existing output with and without timestamps", () => {
  const plain = makeLocalResult(sample);
  assert.equal(plain.content, "Hola, ¿qué tal? Todo bien.\n");
  assert.equal(plain.preview, plain.content);
  assert.equal(plain.filename, "transcript-entrevista.txt");
  assert.match(plain.mime, /^text\/plain(?:;|$)/);

  const timed = makeLocalResult({ ...sample, timestamps: true });
  assert.equal(timed.content, "[00:00] Hola, ¿qué tal?\n[00:01] Todo bien.\n");
  const long = makeLocalResult({
    ...sample,
    timestamps: true,
    duration: 3720,
    segments: [{ start: 3663.9, end: 3665, text: " Texto. " }],
  });
  assert.equal(long.content, "[1:01:03] Texto.\n");
});

test("Markdown keeps transcript markup as readable text", () => {
  const result = makeLocalResult({
    ...sample,
    format: "md",
    text: "# Título <script>alert(1)</script> [enlace](javascript:alert(1))",
  });
  assert.match(result.filename, /\.md$/);
  assert.match(result.mime, /^text\/markdown(?:;|$)/);
  assert.match(result.content, /Título/);
  assert.doesNotMatch(result.content, /^# Título/m);
  assert.doesNotMatch(result.content, /(?<!\\)<script>/i);
  assert.doesNotMatch(result.content, /\[enlace\]\(javascript:/i);
  assert.equal(result.preview, result.content);
});

test("SRT creates numbered cues even when the timestamps option is off", () => {
  const result = makeLocalResult({ ...sample, format: "srt" });
  assert.match(result.filename, /\.srt$/);
  assert.match(result.mime, /^application\/x-subrip(?:;|$)/);
  assert.match(result.content, /^1\r?\n00:00:00,250 --> 00:00:01,200\r?\nHola, ¿qué tal\?/);
  assert.match(result.content, /\r?\n\r?\n2\r?\n00:00:01,500 --> 00:00:03,000\r?\nTodo bien\./);
  assert.equal(result.preview, undefined);
});

test("WebVTT fills missing ends and escapes cue markup", () => {
  const result = makeLocalResult({
    ...sample,
    format: "vtt",
    duration: 2,
    segments: [
      { start: 0, end: null, text: " Uno <dos> & tres --> cuatro. " },
      { start: 1.25, end: null, text: " El final. " },
    ],
  });
  assert.match(result.filename, /\.vtt$/);
  assert.match(result.mime, /^text\/vtt(?:;|$)/);
  assert.match(result.content, /^WEBVTT\r?\n\r?\n/);
  assert.match(result.content, /00:00:00\.000 --> 00:00:01\.250/);
  assert.match(result.content, /00:00:01\.250 --> 00:00:02\.000/);
  assert.match(result.content, /Uno &lt;dos&gt; &amp; tres/);
  assert.equal(result.content.match(/-->/g).length, 2);
  assert.equal(result.preview, undefined);
});

test("subtitle exports reject a transcript with no known cue starts", () => {
  for (const format of ["srt", "vtt"]) {
    assert.throws(
      () => makeLocalResult({ ...sample, format, segments: [{ start: null, end: null, text: "Hola" }] }),
      { code: "missingTiming" },
    );
  }
});

test("DOCX passes clean timestamped paragraphs to the document writer", () => {
  const bytes = Uint8Array.from([80, 75, 3, 4]);
  let written;
  const result = makeLocalResult({
    ...sample,
    source: ' carpeta\\archivo?<prueba>\u0000.mp3',
    format: "docx",
    timestamps: true,
    segments: [{ start: 0, end: 1, text: " Una señal\u0000 con < y &. " }],
  }, (paragraphs) => {
    written = paragraphs;
    return bytes;
  });
  assert.match(result.filename, /\.docx$/);
  assert.doesNotMatch(result.filename, /[<>:"/\\|?*\x00-\x1f]/);
  assert.equal(result.mime, "application/vnd.openxmlformats-officedocument.wordprocessingml.document");
  assert.equal(result.content, bytes);
  assert.equal(result.preview, undefined);
  assert.ok(written.includes("[00:00] Una señal con < y &."));
  assert.ok(written.every((paragraph) => !paragraph.includes("\u0000")));
});
