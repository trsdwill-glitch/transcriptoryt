const mimeTypes = {
  txt: "text/plain;charset=utf-8",
  md: "text/markdown;charset=utf-8",
  docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  srt: "application/x-subrip;charset=utf-8",
  vtt: "text/vtt;charset=utf-8",
};

function isTime(value) {
  return Number.isFinite(value) && value >= 0;
}

function filenameFor(source, format) {
  const clean = (source ?? "").normalize("NFC").replace(/\.[^.]+$/, "")
    .replace(/[\p{Cc}\p{Cf}\p{Cs}]/gu, "").replace(/[<>:"/\\|?*]/g, "_");
  // Leave room for the prefix and extension even with four UTF-8 bytes per character.
  const name = Array.from(clean).slice(0, 59).join("").trim().replace(/[. ]+$/, "");
  return `transcript-${name || "audio"}.${format}`;
}

function cleanText(text) {
  return text.replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f\ufffe\uffff\p{Cs}]/gu, "");
}

function escapeMarkup(text) {
  return text.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

function escapeMarkdown(text) {
  return escapeMarkup(cleanText(text)).replace(/[\\`*_[\]~#+\-.!|=]/g, "\\$&");
}

function stamp(seconds, showHours) {
  const total = Math.floor(Math.max(0, seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor(total / 60) % 60;
  const clock = [minutes, total % 60].map((part) => String(part).padStart(2, "0")).join(":");
  return showHours ? `${hours}:${clock}` : clock;
}

function timedLines(segments, duration) {
  const lines = [];
  for (const segment of segments) {
    const text = segment.text.trim();
    if (!text) continue;
    const mark = Number.isFinite(segment.start) ? `[${stamp(segment.start, duration >= 3600)}]` : "";
    lines.push({ mark, text });
  }
  return lines;
}

function captionTime(milliseconds, separator) {
  const hours = Math.floor(milliseconds / 3600000);
  const minutes = Math.floor(milliseconds / 60000) % 60;
  const seconds = Math.floor(milliseconds / 1000) % 60;
  const clock = [hours, minutes, seconds].map((part) => String(part).padStart(2, "0")).join(":");
  return `${clock}${separator}${String(milliseconds % 1000).padStart(3, "0")}`;
}

function wrapCaption(text) {
  const lines = [];
  let line = "";
  for (const word of text.split(" ")) {
    if (line && line.length + word.length + 1 > 42) {
      lines.push(line);
      line = "";
    }
    line = line ? `${line} ${word}` : word;
  }
  if (line) lines.push(line);
  return lines;
}

function makeCaptions(segments, duration, format) {
  const filtered = segments.filter(({ start, text }) => isTime(start) && text.trim());
  const sorted = filtered.map((segment) => ({
    ...segment,
    text: cleanText(segment.text).replace(/\s+/gu, " ").replaceAll("-->", "→").trim(),
  })).filter(({ text }) => text).sort((a, b) => a.start - b.start);
  const durationMs = duration > 0 && isTime(duration) ? Math.round(duration * 1000) : Infinity;
  const separator = format === "srt" ? "," : ".";
  const cues = [];
  for (const [index, segment] of sorted.entries()) {
    const start = Math.round(segment.start * 1000);
    if (start >= durationMs) continue;
    const next = sorted[index + 1];
    const end = isTime(segment.end) && segment.end > segment.start ? segment.end
      : next && next.start > segment.start ? next.start
        : Number.isFinite(durationMs) ? durationMs / 1000 : segment.start + 0.5;
    const endMs = Math.min(durationMs, Math.max(start + 1, Math.round(end * 1000)));
    const lines = wrapCaption(segment.text);
    const groups = Math.min(Math.ceil(lines.length / 2), endMs - start);
    for (let group = 0; group < groups; group += 1) {
      const a = start + Math.round((endMs - start) * group / groups);
      const b = start + Math.round((endMs - start) * (group + 1) / groups);
      const first = Math.floor(lines.length * group / groups);
      const last = Math.floor(lines.length * (group + 1) / groups);
      cues.push({ start: a, end: b, text: lines.slice(first, last).map(escapeMarkup).join("\n") });
    }
  }
  if (!cues.length) {
    const error = new Error("The transcript has no valid subtitle timing.");
    error.code = "missingTiming";
    throw error;
  }
  cues.sort((a, b) => a.start - b.start);
  const blocks = cues.map(({ start, end, text }, index) => {
    const indexLine = format === "srt" ? `${index + 1}\n` : "";
    const times = `${captionTime(start, separator)} --> ${captionTime(end, separator)}`;
    return `${indexLine}${times}\n${text}`;
  });
  return `${format === "vtt" ? "WEBVTT\n\n" : ""}${blocks.join("\n\n")}\n`;
}

export function makeLocalResult({ source, format, timestamps, text, segments = [], duration }, makeDocument) {
  if (!Object.hasOwn(mimeTypes, format)) throw new Error("Unsupported format.");
  const filename = filenameFor(source, format);
  const mime = mimeTypes[format];
  if (format === "srt" || format === "vtt") {
    return { filename, mime, content: makeCaptions(segments, duration, format) };
  }

  const lines = timestamps ? timedLines(segments, duration) : [];
  const body = lines.length ? lines : [{ mark: "", text: text.trim() }];
  const markText = ({ mark, text }) => mark ? `${mark} ${text}` : text;
  if (format === "docx") {
    return { filename, mime, content: makeDocument(body.map((line) => cleanText(markText(line)))) };
  }

  const content = format === "md"
    ? body.map(({ mark, text }) => `${mark ? `${escapeMarkdown(mark)} ` : ""}${escapeMarkdown(text)}`).join("\n\n") + "\n"
    : body.map(markText).join("\n") + "\n";
  return { filename, mime, content, preview: content };
}
