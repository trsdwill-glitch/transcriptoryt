(() => {
"use strict";

const storageKey = "transcriptor.preferences";
const defaults = { format: "txt", timestamps: true, language: "auto", output: "separate" };
const query = new URLSearchParams(location.search);
const mock = query.get("mock") === "1";
const apiFetch = mock ? createMockApi() : (path, options) => fetch(path, options);
const fingerprint = clientFingerprint();
const jobStages = {
  submitting: "submitting", queued: "stageQueued", checking: "stageChecking",
  downloading: "stageDownloading", transcribing: "stageTranscribing", formatting: "stageFormatting",
  localReading: "localReading", localLoading: "localLoading", localInitializing: "localInitializing",
  done: "stageDone", error: "stageError", cancelled: "stageCancelled",
};
const itemStages = {
  pending: "itemPending", downloading: "itemDownloading", transcribing: "itemTranscribing",
  done: "itemDone", error: "itemError",
};
const activeStages = new Set(["submitting", "queued", "checking", "downloading", "transcribing", "formatting", "localReading", "localLoading", "localInitializing"]);
const subtitleFormats = new Set(["srt", "vtt"]);
const $ = (id) => document.getElementById(id);
const form = $("job-form");
const localForm = $("local-form");
const localFileInput = $("local-file");
const localFormatInput = $("local-format");
const localLanguageInput = $("local-language");
const localTimestampsInput = $("local-timestamps");
const localSubmitButton = $("local-submit");
const modeButtons = [...document.querySelectorAll("[data-mode]")];
// iPadOS can report the desktop Mac user agent.
const compactDevice = navigator.userAgentData?.mobile || /Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent)
  || (navigator.maxTouchPoints > 1 && /Mac/.test(navigator.platform))
  || (navigator.deviceMemory > 0 && navigator.deviceMemory <= 4);
const mobileMaxSeconds = 10 * 60;
const mobileMaxBytes = 24 * 1024 * 1024;
const interruptedKey = "transcriptoryt.local-in-progress";
if (compactDevice) $("local-help").dataset.i18n = "localMobileHelp";
const localSupported = isSecureContext && location.protocol !== "file:" && typeof Worker === "function"
  && typeof OfflineAudioContext === "function";
const linkEntries = $("link-entries");
const fileInput = $("file-input");
const dropArea = $("drop-area");
const formatInput = $("format");
const languageInput = $("language");
const timestampsInput = $("timestamps");
const outputInput = $("output");
const submitButton = $("submit-button");
const fileList = $("file-list");
const cancelButton = $("cancel");
const downloadButton = $("download");
const copyButton = $("copy");
const previewText = $("preview-text");
const files = [];
const saved = readPreferences();
const preferences = { ...defaults, ...saved };
const pageKind = document.documentElement.dataset.tool;
const uiLanguage = document.documentElement.lang;
const labels = toolCopy(pageKind, uiLanguage);
let config;
let quota;
let quotaLoading = true;
let notice;
let currentJob;
let pollTimer;
let polling = false;
let nextLinkId = 2;
let mode = !mock && query.get("mode") === "local" ? "local" : "server";
let worker;
let configLoading;
let localClock;
let localTimestamps = false;

function readPreferences() {
  try {
    const value = JSON.parse(localStorage.getItem(storageKey));
    return value && typeof value === "object" ? value : {};
  } catch {
    return {};
  }
}

function savePreferences() {
  try {
    localStorage.setItem(storageKey, JSON.stringify(preferences));
  } catch {
    // Options remain available for the current page.
  }
}

function t(key, values = {}) {
  return labels[key].replace(/\{(\w+)\}/g, (_, name) => String(values[name] ?? ""));
}

function setText(element, value) {
  if (element.textContent !== value) element.textContent = value;
}

function localNumber(value) {
  return new Intl.NumberFormat(uiLanguage, { maximumFractionDigits: 2 }).format(value);
}

function waitTime(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 0) return t("waitFallback");
  const total = Math.ceil(seconds / 60);
  const hours = Math.floor(total / 60);
  const minutes = total % 60;
  if (!hours) return t("waitMinutes", { minutes });
  return t(minutes ? "waitHoursMinutes" : "waitHours", { hours, minutes });
}

function errorText(error) {
  const p = error.params ?? {};
  if (!Object.hasOwn(labels, error.code)) return error.message || t("INTERNAL");
  return t(error.code, {
    maxItems: p.max_items ?? config?.max_items ?? 5,
    maxMb: p.max_mb ?? config?.max_upload_mb ?? 500,
    maxMin: p.max_min ?? config?.max_file_min ?? 120,
    remaining: localNumber(p.remaining_min ?? quota?.remaining_min ?? 0),
    wait: waitTime(p.resets_in_s),
  });
}

function showNotice(value) {
  notice = value;
  renderNotice();
}

function renderNotice() {
  const element = $("notice");
  element.hidden = !notice;
  setText(element, notice ? typeof notice === "string" ? t(notice) : errorText(notice) : "");
}

function renderQuota() {
  if (mode === "local") {
    setText($("quota"), t("localQuota"));
    return;
  }
  const value = quota?.remaining_min;
  setText($("quota"), Number.isFinite(value)
    ? t("quotaRemaining", { minutes: localNumber(value) })
    : t(quotaLoading ? "quotaLoading" : "quotaUnavailable"));
}

function populateSelect(select, values, chosen, labelFor) {
  select.replaceChildren(...values.map((value) => new Option(labelFor(value), value)));
  if (values.includes(chosen)) select.value = chosen;
}

function renderAudioLanguages() {
  if (!config) {
    languageInput.options[0].textContent = t("auto");
    return;
  }
  const names = new Intl.DisplayNames([uiLanguage], { type: "language" });
  populateSelect(languageInput, config.languages, preferences.language, (code) => {
    if (code === "auto") return t("auto");
    if (code === "ba") return t("bashkir");
    if (code === "bo") return t("tibetan");
    // Whisper uses its own code for Javanese.
    return names.of(code === "jw" ? "jv" : code) || code;
  });
}

function translate() {
  for (const element of document.querySelectorAll("[data-i18n]")) {
    setText(element, t(element.dataset.i18n));
  }
  for (const element of document.querySelectorAll("[data-i18n-label]")) {
    element.setAttribute("aria-label", t(element.dataset.i18nLabel));
  }
  for (const element of document.querySelectorAll("[data-i18n-placeholder]")) {
    element.placeholder = t(element.dataset.i18nPlaceholder);
  }
  const theme = $("theme-toggle");
  theme.dataset.switchDark = t("themeToggleDark");
  theme.dataset.switchLight = t("themeToggleLight");
  document.dispatchEvent(new Event("languagechange"));
  renderAudioLanguages();
  renderLinks();
  renderFiles();
  renderForm();
  renderNotice();
  renderQuota();
  renderJob();
}

async function clientFingerprint() {
  const input = [navigator.userAgent, navigator.language, Intl.DateTimeFormat().resolvedOptions().timeZone,
    `${screen.width}x${screen.height}`, screen.colorDepth, navigator.hardwareConcurrency].join(" | ");
  if (globalThis.crypto?.subtle) {
    try {
      const bytes = new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(input)));
      return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
    } catch {
      // Use the same fallback as an insecure browser context.
    }
  }
  return Array.from({ length: 8 }, (_, index) => {
    let hash = 2166136261 ^ index;
    for (let i = 0; i < input.length; i += 1) hash = Math.imul(hash ^ input.charCodeAt(i), 16777619);
    return (hash >>> 0).toString(16).padStart(8, "0");
  }).join("");
}

class ApiError extends Error {
  constructor(detail) {
    super(detail.message || detail.code);
    this.detail = detail;
  }
}

async function request(path, options = {}) {
  const headers = new Headers(options.headers);
  headers.set("X-Client-Fp", await fingerprint);
  const response = await apiFetch(path, {
    credentials: "same-origin", cache: "no-store", ...options, headers,
    signal: options.method === "POST" ? undefined : AbortSignal.timeout(20000),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(body.error ?? { code: response.status === 413 ? "TOO_LARGE" : "INTERNAL" });
  }
  return response;
}

async function json(path, options) {
  return (await request(path, options)).json();
}

function urlInputs() {
  return Array.from(linkEntries.querySelectorAll("input"));
}

function filledLinks() {
  return urlInputs().filter((input) => input.value.trim());
}

function isBusy() {
  return activeStages.has(currentJob?.status);
}

function renderLinks() {
  const rows = Array.from(linkEntries.children);
  for (const [index, row] of rows.entries()) {
    const number = index + 1;
    row.querySelector("label").textContent = t(index === 0 ? "sourceLabel" : "extraSourceLabel", { number });
    row.querySelector("input").placeholder = t("sourcePlaceholder");
    row.querySelector("button[type=button]")?.setAttribute("aria-label", t("removeLink", { number }));
  }
}

function addLink() {
  if (!config || isBusy() || urlInputs().length + files.length >= config.max_items) return;
  const row = $("link-template").content.firstElementChild.cloneNode(true);
  const input = row.querySelector("input");
  input.id = `source-${nextLinkId++}`;
  row.querySelector("label").htmlFor = input.id;
  row.querySelector("button").addEventListener("click", () => {
    const previous = row.previousElementSibling.querySelector("input");
    row.remove();
    inputsChanged();
    renderLinks();
    previous.focus();
  });
  linkEntries.append(row);
  renderLinks();
  inputsChanged();
  input.focus();
}

function receiveFiles(incoming) {
  if (!config || isBusy()) return;
  const limit = config.max_upload_mb * 1024 * 1024;
  const selected = Array.from(incoming);
  if (!selected.length) return;
  let problem;
  for (const file of selected) {
    if (file.size > limit) {
      problem ??= { code: "TOO_LARGE" };
    } else if (filledLinks().length + files.length >= config.max_items) {
      problem ??= { code: "BATCH_TOO_LARGE" };
    } else {
      files.push(file);
    }
  }
  renderFiles();
  renderForm();
  showNotice(problem);
}

function renderFiles() {
  fileList.replaceChildren(...files.map((file, index) => {
    const row = document.createElement("li");
    const name = document.createElement("span");
    name.textContent = file.name;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "icon-button remove-button";
    button.textContent = "×";
    button.setAttribute("aria-label", t("removeFile", { name: file.name }));
    button.addEventListener("click", () => {
      files.splice(index, 1);
      renderFiles();
      inputsChanged();
      const sibling = fileList.children[Math.min(index, files.length - 1)];
      (sibling?.querySelector("button") ?? $("choose-files")).focus();
    });
    row.append(name, button);
    return row;
  }));
  fileList.hidden = files.length === 0;
}

function renderForm() {
  const busy = isBusy();
  form.hidden = mode !== "server";
  localForm.hidden = mode !== "local";
  for (const button of modeButtons) {
    button.disabled = busy;
    button.setAttribute("aria-pressed", String(button.dataset.mode === mode));
  }
  const localLocked = busy || !localSupported;
  const needsTiming = subtitleFormats.has(localFormatInput.value);
  localFileInput.disabled = localLanguageInput.disabled = localFormatInput.disabled = localLocked;
  localTimestampsInput.disabled = localLocked || needsTiming;
  localTimestampsInput.checked = needsTiming || localTimestamps;
  localSubmitButton.disabled = busy || !localSupported || !localFileInput.files.length;
  const locked = busy || !config;
  const count = filledLinks().length + files.length;
  const tooMany = config && count > config.max_items;
  const format = formatInput.value;
  const subtitles = subtitleFormats.has(format);
  const canCombine = config?.combinable.includes(format) ?? false;
  const full = config && count >= config.max_items;
  for (const element of form.querySelectorAll("input, select, button")) element.disabled = locked;
  // The first field can be filled while the configuration is loading.
  $("source-1").disabled = busy;
  $("add-link").disabled = locked || urlInputs().length + files.length >= config.max_items;
  $("upload-button").disabled = $("choose-files").disabled = fileInput.disabled = locked || full;
  submitButton.disabled = locked || Boolean(tooMany);
  timestampsInput.checked = subtitles || preferences.timestamps !== false;
  timestampsInput.disabled = locked || subtitles;
  $("combine-option").hidden = count < 2;
  $("combine-hint").hidden = canCombine || count < 2;
  outputInput.options[1].disabled = !canCombine;
  outputInput.value = canCombine && preferences.output === "combined" ? "combined" : "separate";
  setText($("upload-help"), t("uploadHelp", {
    maxItems: config?.max_items ?? 5, maxMb: config?.max_upload_mb ?? 500, maxMin: config?.max_file_min ?? 120,
  }));
}

function inputsChanged() {
  const overLimit = config && filledLinks().length + files.length > config.max_items;
  showNotice(overLimit ? { code: "BATCH_TOO_LARGE" } : undefined);
  renderForm();
}

function basePath(job) {
  return `/api/jobs/${encodeURIComponent(job.id)}`;
}

function visibleJob() {
  return currentJob?.mode === mode ? currentJob : undefined;
}

function showLocalTiming(job) {
  const element = $("local-timing");
  element.hidden = job?.mode !== "local" || (!isBusy() && job.status !== "done");
  if (element.hidden) return;
  const now = performance.now();
  const elapsed = Math.floor(((job.finishedAt ?? now) - job.startedAt) / 1000);
  const minutes = Math.floor(elapsed / 60);
  const seconds = String(elapsed % 60).padStart(2, "0");
  const details = [t(job.status === "done" ? "localFinished" : "localElapsed", { time: `${minutes}:${seconds}` })];
  if (job.completed >= 3 && job.completed < job.total) {
    const secondsPerSection = (job.lastSectionAt - job.firstSectionAt) / 1000 / (job.completed - 1);
    const remaining = secondsPerSection * (job.total - job.completed);
    if (remaining > 0 && now - job.lastSectionAt < Math.max(30000, secondsPerSection * 3000)) {
      details.push(t("localRemaining", { time: waitTime(remaining) }));
    }
  } else if (job.duration >= 600 && job.completed < 3) {
    details.push(t("localLongAudio"));
  }
  setText(element, details.join(" · "));
}

function renderJob() {
  const job = visibleJob();
  $("job-panel").hidden = !job;
  $("result-panel").hidden = !job?.result;
  showLocalTiming(job);
  if (!job) return;
  const queued = job.status === "queued" && job.queuePosition > 0;
  const stage = job.mode === "local" && job.status === "transcribing"
    ? job.engine === "wasm" ? "localTranscribingWasm" : "localTranscribing"
    : jobStages[job.status] ?? "stageError";
  let stageText = queued ? t("stageQueuePosition", { position: job.queuePosition }) : t(stage);
  if (job.status === "transcribing" && job.mode === "local" && job.total > 1) {
    stageText += ` · ${t("localChunks", { completed: job.completed, total: job.total })}`;
  }
  setText($("job-stage"), stageText);
  const progress = $("progress");
  if (job.mode === "local" && isBusy() && !Number.isFinite(job.progress)) {
    progress.removeAttribute("value");
    setText($("progress-percent"), "");
  } else {
    progress.value = job.progress ?? 0;
    setText($("progress-percent"), t("statusPercent", { percent: Math.round(progress.value * 100) }));
  }
  cancelButton.hidden = !isBusy();
  cancelButton.disabled = job.cancelRequested;
  setText(cancelButton, t(job.cancelRequested ? "cancelling" : "cancel"));
  const items = $("job-items");
  items.hidden = job.items.length === 0;
  items.replaceChildren(...job.items.map((item) => {
    const row = document.createElement("li");
    const line = document.createElement("div");
    line.className = "item-line";
    const name = document.createElement("span");
    name.className = "item-name";
    name.textContent = item.title || item.source || t("itemFallback", { number: item.index + 1 });
    const status = document.createElement("span");
    status.className = "item-state";
    status.textContent = t(itemStages[item.status] ?? "itemPending");
    line.append(name, status);
    row.append(line);
    if (item.error) {
      const detail = document.createElement("p");
      detail.className = "item-error";
      detail.textContent = errorText(item.error);
      row.append(detail);
    }
    return row;
  }));
  const failures = job.items.filter((item) => item.error).length;
  const partial = Boolean(job.result && failures);
  $("partial").hidden = !partial;
  setText($("partial"), partial ? t("partial", { count: failures }) : "");
  if (job.result) renderResult(job);
}

function renderResult(job) {
  const extension = job.result.filename.split(".").pop().toLowerCase();
  setText(downloadButton, t("download", { format: extension.toUpperCase() }));
  downloadButton.disabled = Boolean(job.downloading);
  const showPreview = ["txt", "md"].includes(extension);
  $("preview").hidden = !showPreview;
  copyButton.hidden = !showPreview || job.preview === undefined;
  if (previewText.value !== (job.preview ?? "")) previewText.value = job.preview ?? "";
  setText($("result-feedback"), job.feedback ? t(job.feedback) : "");
}

function schedulePoll(job) {
  clearTimeout(pollTimer);
  if (currentJob === job && job?.id && isBusy() && !polling && !job.cancelling) {
    pollTimer = setTimeout(() => poll(job), document.hidden ? 2500 : 1500);
  }
}

function updateJob(job, snapshot) {
  Object.assign(job, {
    status: snapshot.status, items: snapshot.items, progress: snapshot.progress,
    queuePosition: snapshot.queue_position, result: snapshot.result,
  });
  quota = snapshot.quota ?? quota;
  renderQuota();
  if (!isBusy()) {
    job.cancelRequested = false;
    showNotice(snapshot.status === "error" ? snapshot.error ?? { code: "INTERNAL" } : undefined);
    if (snapshot.status === "done" && !snapshot.result) showNotice({ code: "RESULT_EXPIRED" });
    void refreshQuota();
    if (job.result && /\.(txt|md)$/i.test(job.result.filename)) void loadPreview(job);
  } else if (notice === "connectionError") {
    showNotice();
  }
  renderForm();
  renderJob();
}

async function poll(job) {
  if (currentJob !== job || !isBusy() || polling || job.cancelling) return;
  polling = true;
  try {
    const snapshot = await json(basePath(job));
    if (!job.cancelling && currentJob === job && isBusy()) updateJob(job, snapshot);
  } catch (error) {
    if (!job.cancelling && currentJob === job && isBusy()) {
      showNotice(error instanceof ApiError ? error.detail : "connectionError");
      if (error.detail?.code === "NOT_FOUND") {
        job.status = "error";
        renderForm();
        renderJob();
        void refreshQuota();
      }
    }
  } finally {
    polling = false;
    schedulePoll(currentJob);
  }
}

async function refreshQuota() {
  try {
    quota = await json("/api/quota");
  } catch {
    // Preserve the last available quota from the job response.
  } finally {
    quotaLoading = false;
    renderQuota();
  }
}

async function submit(event) {
  event.preventDefault();
  if (mode !== "server" || !config || isBusy()) return;
  const inputs = filledLinks();
  const urls = inputs.map((input) => input.value.trim());
  const badLink = urls.findIndex((value) => {
    try {
      return !/^https?:\/\//i.test(value) || !new URL(value).hostname;
    } catch {
      return true;
    }
  });
  if (badLink !== -1) {
    showNotice({ code: "URL_INVALID" });
    inputs[badLink].focus();
    return;
  }
  const count = urls.length + files.length;
  if (count > config.max_items || !count) {
    showNotice({ code: count ? "BATCH_TOO_LARGE" : "NO_INPUT" });
    return;
  }
  const data = new FormData();
  data.set("urls", JSON.stringify(urls));
  for (const file of files) data.append("files", file, file.name);
  data.set("format", formatInput.value);
  data.set("language", languageInput.value);
  data.set("timestamps", String(timestampsInput.checked));
  data.set("combine", String(count > 1 && outputInput.value === "combined"));
  const job = { mode: "server", status: "submitting", items: [], cancelRequested: false };
  currentJob = job;
  showNotice();
  renderForm();
  renderJob();
  try {
    const created = await json("/api/jobs", { method: "POST", body: data });
    job.id = created.job_id;
    job.items = created.items;
    job.status = "queued";
    renderJob();
    if (job.cancelRequested) await sendCancellation(job);
    else schedulePoll(job);
  } catch (error) {
    job.status = "error";
    showNotice(error instanceof ApiError ? error.detail : "connectionError");
    void refreshQuota();
    renderForm();
    renderJob();
  }
}

async function sendCancellation(job) {
  clearTimeout(pollTimer);
  job.cancelling = true;
  try {
    await json(basePath(job), { method: "DELETE" });
    // The job may have finished just before the server handled the cancellation.
    updateJob(job, await json(basePath(job)));
  } catch (error) {
    showNotice(error instanceof ApiError ? error.detail : "connectionError");
  } finally {
    job.cancelling = false;
    job.cancelRequested = false;
    renderJob();
    schedulePoll(job);
  }
}

function downloadPath(job) {
  const expected = `${basePath(job)}/download`;
  if (job.result.download_url !== expected) throw new ApiError({ code: "NOT_FOUND" });
  return expected;
}

async function loadPreview(job) {
  job.feedback = "previewLoading";
  renderResult(job);
  try {
    job.preview = await (await request(downloadPath(job))).text();
    job.feedback = undefined;
  } catch {
    job.feedback = "previewFailed";
  }
  if (visibleJob() === job) renderResult(job);
}

async function downloadResult() {
  const job = visibleJob();
  if (!job?.result || job.downloading) return;
  job.downloading = true;
  renderResult(job);
  try {
    const blob = job.mode === "local" ? new Blob([job.result.content], { type: job.result.mime })
      : await (await request(downloadPath(job))).blob();
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = job.result.filename;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) {
    if (visibleJob() === job) showNotice(error instanceof ApiError ? error.detail : "connectionError");
  } finally {
    job.downloading = false;
    if (visibleJob() === job) renderResult(job);
  }
}

async function copyPreview() {
  const job = visibleJob();
  if (job?.preview === undefined) return;
  try {
    await navigator.clipboard.writeText(job.preview);
    job.feedback = "copied";
  } catch {
    previewText.focus();
    previewText.select();
    try {
      job.feedback = document.execCommand("copy") ? "copied" : "copyFailed";
    } catch {
      job.feedback = "copyFailed";
    }
  }
  if (visibleJob() === job) renderResult(job);
}

function getWorker() {
  if (worker) return worker;
  const instance = new Worker("/assets/local-whisper-worker.js?v=20261124", { type: "module" });
  instance.addEventListener("message", ({ data }) => {
    const job = currentJob;
    if (instance !== worker || job?.mode !== "local" || !isBusy()) return;
    switch (data.stage) {
      case "loading":
        job.status = "localLoading";
        job.progress = data.progress;
        break;
      case "initializing":
        job.status = "localInitializing";
        job.progress = undefined;
        break;
      case "ready":
        void decodeAndSend(job);
        return;
      case "transcribing":
        job.status = "transcribing";
        job.engine = data.engine;
        job.completed = data.completed;
        job.total = data.total;
        if (data.completed > 0) {
          job.lastSectionAt = performance.now();
          job.firstSectionAt ??= job.lastSectionAt;
        }
        job.progress = data.completed > 0 ? Math.min(0.99, data.completed / data.total) : undefined;
        break;
      case "done":
        void completeLocalJob(job, data);
        return;
      case "fallback":
        stopWorker();
        job.status = "localLoading";
        job.progress = undefined;
        job.completed = 0;
        job.firstSectionAt = undefined;
        try {
          sendLocalAudio(job, data.audio, "wasm");
        } catch {
          finishLocalJob(job, "error", "localFailed");
          return;
        }
        break;
      case "error":
        finishLocalJob(job, "error", data.reason === "empty" ? "localNoSpeech"
          : data.reason === "runtime" ? "localRuntimeMissing" : "localFailed");
        return;
      default:
        return;
    }
    renderJob();
  });
  instance.addEventListener("error", (event) => {
    event.preventDefault();
    if (instance === worker && currentJob?.mode === "local" && isBusy()) {
      finishLocalJob(currentJob, "error", "localFailed");
    }
  });
  worker = instance;
  return instance;
}

function stopWorker() {
  worker?.terminate();
  worker = undefined;
}

function sendLocalAudio(job, audio, device) {
  getWorker().postMessage({ audio, ...job.options, device, compact: compactDevice }, [audio.buffer]);
}

async function completeLocalJob(job, data) {
  job.status = "formatting";
  renderJob();
  try {
    const { makeLocalResult } = await import("./local-formats.mjs");
    if (currentJob !== job || job.status !== "formatting") return;
    const output = {
      source: job.source, format: job.format, timestamps: job.options.timestamps,
      text: data.text, segments: data.segments, duration: job.duration,
    };
    let message;
    try {
      job.result = makeLocalResult(output, makeDocument);
    } catch (error) {
      if (error.code !== "missingTiming" || !data.text) throw error;
      job.result = makeLocalResult({ ...output, format: "txt", timestamps: false }, makeDocument);
      message = "localMissingTiming";
    }
    job.preview = job.result.preview;
    job.progress = 1;
    finishLocalJob(job, "done", message);
  } catch (error) {
    if (currentJob === job && job.status === "formatting") {
      finishLocalJob(job, "error", "localFormatFailed");
    }
  }
}

function finishLocalJob(job, status, error) {
  clearInterval(localClock);
  if (compactDevice || status !== "done") stopWorker();
  if (compactDevice) {
    try { sessionStorage.removeItem(interruptedKey); } catch { /* Storage is optional. */ }
  }
  job.file = undefined;
  job.finishedAt = performance.now();
  job.status = status;
  showNotice(error);
  renderForm();
  renderJob();
}

async function readLocalAudio(file) {
  const sampleRate = 16000;
  const context = new OfflineAudioContext(1, 1, sampleRate);
  const decoded = await context.decodeAudioData(await file.arrayBuffer());
  const channels = decoded.numberOfChannels;
  if (!decoded.length || !channels) throw new Error("Empty audio");
  const audio = decoded.getChannelData(0);
  if (channels === 1) return audio;
  // Mix in place to avoid another full-size PCM allocation for long files.
  for (let i = 0; i < audio.length; i += 1) audio[i] /= channels;
  for (let channel = 1; channel < channels; channel += 1) {
    const samples = decoded.getChannelData(channel);
    for (let i = 0; i < samples.length; i += 1) audio[i] += samples[i] / channels;
  }
  return audio;
}

async function checkMobileAudio(file) {
  if (file.size > mobileMaxBytes) return "localMobileLimit";
  const media = document.createElement("audio");
  const url = URL.createObjectURL(file);
  try {
    const duration = await new Promise((resolve) => {
      const timer = setTimeout(() => finish(NaN), 5000);
      function finish(value) {
        clearTimeout(timer);
        resolve(value);
      }
      media.onloadedmetadata = () => finish(media.duration);
      media.onerror = () => finish(NaN);
      media.preload = "metadata";
      media.src = url;
    });
    if (Number.isFinite(duration) && duration > 0) return duration > mobileMaxSeconds ? "localMobileLimit" : undefined;
    // Some short MediaRecorder clips do not carry a duration in their header.
    return file.size <= 1024 * 1024 ? undefined : "localDurationUnknown";
  } finally {
    media.onerror = media.onloadedmetadata = null;
    media.removeAttribute("src");
    media.load();
    URL.revokeObjectURL(url);
  }
}

async function submitLocal(event) {
  event.preventDefault();
  const file = localFileInput.files[0];
  if (mode !== "local" || !file || isBusy() || !localSupported) return;
  const job = {
    mode: "local", source: file.name, file, format: localFormatInput.value, status: "localReading", items: [],
    startedAt: performance.now(), completed: 0,
    options: { language: localLanguageInput.value, timestamps: localTimestampsInput.checked },
  };
  currentJob = job;
  showNotice();
  renderForm();
  renderJob();
  localClock = setInterval(() => showLocalTiming(visibleJob()), 1000);
  if (compactDevice) {
    try { sessionStorage.setItem(interruptedKey, "1"); } catch { /* Storage is optional. */ }
    try {
      const problem = await checkMobileAudio(file);
      if (currentJob !== job || !isBusy()) return;
      if (problem) {
        finishLocalJob(job, "error", problem);
        return;
      }
      // Initialize first so decoded PCM is not resident during model allocation.
      job.status = "localLoading";
      renderJob();
      getWorker().postMessage({ prepare: true, compact: true, device: "wasm" });
    } catch {
      if (currentJob === job && isBusy()) finishLocalJob(job, "error", "localFailed");
    }
    return;
  }
  await decodeAndSend(job);
}

async function decodeAndSend(job) {
  job.status = "localReading";
  renderJob();
  let audio;
  try {
    audio = await readLocalAudio(job.file);
  } catch {
    if (currentJob === job && isBusy()) finishLocalJob(job, "error", "localDecodeFailed");
    return;
  }
  if (currentJob !== job || !isBusy()) return;
  job.duration = audio.length / 16000;
  if (compactDevice && job.duration > mobileMaxSeconds) {
    finishLocalJob(job, "error", "localMobileLimit");
    return;
  }
  job.status = compactDevice ? "transcribing" : "localLoading";
  renderJob();
  try {
    sendLocalAudio(job, audio, query.get("engine") === "wasm" ? "wasm" : "auto");
  } catch {
    finishLocalJob(job, "error", "localFailed");
  }
}

function loadServerConfig() {
  if (configLoading || config) return;
  if (location.protocol === "file:" && !mock) {
    quotaLoading = false;
    renderQuota();
    showNotice("insecureFileMode");
    return;
  }
  configLoading = true;
  void (async () => {
    try {
      const loaded = await json("/api/config");
      // Establish the cookie before enabling job submission.
      await refreshQuota();
      config = loaded;
      populateSelect(formatInput, config.formats, preferences.format, (value) => value.toUpperCase());
      renderAudioLanguages();
      renderForm();
    } catch {
      config = undefined;
      quotaLoading = false;
      renderQuota();
      if (mode === "server") showNotice("loadConfigFailed");
    } finally {
      configLoading = false;
    }
  })();
}

function chooseMode(value) {
  if (isBusy()) return;
  mode = value;
  showNotice(mode === "local" && !localSupported ? "localUnsupported" : undefined);
  renderQuota();
  renderForm();
  renderJob();
  if (mode === "server") loadServerConfig();
}

form.addEventListener("submit", submit);
localForm.addEventListener("submit", submitLocal);
localFormatInput.addEventListener("change", renderForm);
localTimestampsInput.addEventListener("change", () => {
  localTimestamps = localTimestampsInput.checked;
});
localFileInput.addEventListener("change", () => {
  showNotice();
  renderForm();
});
for (const button of modeButtons) {
  button.addEventListener("click", () => chooseMode(button.dataset.mode));
}
linkEntries.addEventListener("input", inputsChanged);
$("add-link").addEventListener("click", addLink);
for (const button of [$("upload-button"), $("choose-files")]) {
  button.addEventListener("click", () => fileInput.click());
}
fileInput.addEventListener("change", () => {
  receiveFiles(fileInput.files);
  fileInput.value = "";
});
dropArea.addEventListener("dragover", (event) => {
  if (!event.dataTransfer.types.includes("Files")) return;
  event.preventDefault();
  if (!isBusy() && config) dropArea.classList.add("is-dragging");
});
dropArea.addEventListener("dragleave", (event) => {
  if (!dropArea.contains(event.relatedTarget)) dropArea.classList.remove("is-dragging");
});
dropArea.addEventListener("drop", (event) => {
  event.preventDefault();
  dropArea.classList.remove("is-dragging");
  receiveFiles(event.dataTransfer.files);
});
for (const input of [formatInput, languageInput, timestampsInput, outputInput]) {
  input.addEventListener("change", () => {
    preferences[input.id] = input.type === "checkbox" ? input.checked : input.value;
    savePreferences();
    renderForm();
  });
}
cancelButton.addEventListener("click", () => {
  const job = currentJob;
  if (!isBusy() || job.cancelRequested) return;
  if (job.mode === "local") {
    finishLocalJob(job, "cancelled");
    return;
  }
  job.cancelRequested = true;
  renderJob();
  if (job.id) void sendCancellation(job);
});
downloadButton.addEventListener("click", downloadResult);
copyButton.addEventListener("click", copyPreview);
document.addEventListener("visibilitychange", () => schedulePoll(currentJob));
$("local-language").value = ["auto", "en", "es", "pt"].includes(preferences.language) ? preferences.language : "auto";
document.querySelector(".mode-switch").hidden = mock || location.protocol === "file:";
translate();
chooseMode(mode);
if (pageKind === "youtube" && mode === "server" && !location.hash) {
  $("source-1").focus({ preventScroll: true });
}
if (compactDevice) {
  try {
    if (sessionStorage.getItem(interruptedKey)) {
      sessionStorage.removeItem(interruptedKey);
      showNotice("localInterrupted");
    }
  } catch { /* Storage is optional. */ }
}

// Write small, uncompressed ZIP files for Word documents and mock downloads.
function packZip(files) {
    const encode = new TextEncoder();
    const local = [];
    const central = [];
    let offset = 0;
    let directorySize = 0;

    for (const { name, data } of files) {
        const filename = encode.encode(name);
        let crc = 0xffffffff;
        for (const byte of data) {
            crc ^= byte;
            for (let bit = 0; bit < 8; bit += 1) {
                crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0);
            }
        }
        crc ^= 0xffffffff;

        const header = new Uint8Array(30);
        const view = new DataView(header.buffer);
        view.setUint32(0, 0x04034b50, true);
        view.setUint16(4, 20, true);
        view.setUint16(6, 0x0800, true);
        view.setUint16(12, 0x21, true);
        view.setUint32(14, crc, true);
        view.setUint32(18, data.length, true);
        view.setUint32(22, data.length, true);
        view.setUint16(26, filename.length, true);
        local.push(header, filename, data);

        const entry = new Uint8Array(46);
        const entryView = new DataView(entry.buffer);
        entryView.setUint32(0, 0x02014b50, true);
        entryView.setUint16(4, 20, true);
        entry.set(header.subarray(4, 30), 6);
        entryView.setUint32(42, offset, true);
        central.push(entry, filename);
        directorySize += entry.length + filename.length;
        offset += header.length + filename.length + data.length;
    }

    const trailer = new Uint8Array(22);
    const end = new DataView(trailer.buffer);
    end.setUint32(0, 0x06054b50, true);
    end.setUint16(8, files.length, true);
    end.setUint16(10, files.length, true);
    end.setUint32(12, directorySize, true);
    end.setUint32(16, offset, true);
    const zip = new Uint8Array(offset + directorySize + trailer.length);
    let position = 0;
    for (const bytes of [...local, ...central, trailer]) {
        zip.set(bytes, position);
        position += bytes.length;
    }
    return zip;
}

function makeDocument(paragraphs) {
    const encode = new TextEncoder();
    const wordNamespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main";
    const escape = (line) => line.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
    const body = paragraphs.map((line) => `<w:p><w:r><w:t xml:space="preserve">${escape(line)}</w:t></w:r></w:p>`).join("");
    return packZip([
        {
            name: "[Content_Types].xml",
            data: encode.encode('<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'),
        },
        {
            name: "_rels/.rels",
            data: encode.encode('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'),
        },
        {
            name: "word/document.xml",
            data: encode.encode(`<w:document xmlns:w="${wordNamespace}"><w:body>${body}</w:body></w:document>`),
        },
    ]);
}

function createMockApi() {
    const mime = {
        txt: "text/plain",
        md: "text/markdown",
        docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        srt: "application/x-subrip",
        vtt: "text/vtt",
        zip: "application/zip",
    };
    const config = {
        formats: ["txt", "md", "docx", "srt", "vtt"],
        combinable: ["txt", "md", "docx"],
        max_items: 5,
        max_file_min: 120,
        max_upload_mb: 500,
        languages: ("auto en es pt fr de it hi af am ar as az ba be bg bn bo br bs ca cs cy da " +
            "el et eu fa fi fo gl gu ha haw he hr ht hu hy id is ja jw ka kk km kn ko la lb ln " +
            "lo lt lv mg mi mk ml mn mr ms mt my ne nl nn no oc pa pl ps ro ru sa sd si sk sl " +
            "sn so sq sr su sv sw ta te tg th tk tl tr tt uk ur uz vi yi yo zh").split(" "),
    };
    const phases = ["queued", "checking", "downloading", "transcribing", "formatting", "done"];
    const progress = [0, 0.05, 0.24, 0.66, 0.93, 1];
    const duration = 12;
    const resets = 18720;
    const jobs = new Map();
    const encode = new TextEncoder();
    let sequence = 0;
    let active = null;
    let usedSeconds = 0;
    const json = (value, status = 200) => new Response(JSON.stringify(value), {
        status,
        headers: { "Content-Type": "application/json" },
    });
    const error = (code, params = {}) => ({ code, message: code, params });
    const fail = (code, status, params) => json({ error: error(code, params) }, status);
    const quota = () => ({
        limit_min: 120,
        used_min: usedSeconds / 60,
        remaining_min: Math.max(0, 120 - usedSeconds / 60),
        resets_in_s: usedSeconds > 0 ? resets : 0,
    });

    function render(job, items) {
        const words = job.language === "es"
            ? "Esta es una transcripción de ejemplo. Puedes copiarla o descargarla."
            : "This is a sample transcript. You can copy or download it.";
        if (job.format === "srt") return encode.encode(`1\n00:00:00,000 --> 00:00:12,000\n${words}\n`);
        if (job.format === "vtt") return encode.encode(`WEBVTT\n\n00:00:00.000 --> 00:00:12.000\n${words}\n`);

        const content = `${job.timestamps ? "[00:00] " : ""}${words}`;
        const headings = items.map(({ index }) => `${job.language === "es" ? "Ejemplo" : "Sample"} ${index + 1}`);
        if (job.format === "docx") {
            return makeDocument(headings.flatMap((heading) => [heading, content]));
        }
        const prefix = job.format === "md" ? "## " : "";
        return encode.encode(headings.map((heading) => `${prefix}${heading}\n\n${content}\n`).join("\n"));
    }

    function finish(job) {
        const successes = job.items.filter((item) => !job.failed.has(item.index));
        job.completed = Date.now();
        active = null;
        if (!successes.length) {
            job.error = error("UNAVAILABLE");
            return;
        }

        const zipped = successes.length > 1 && !job.combine;
        const extension = zipped ? "zip" : job.format;
        const filename = `sample-transcript.${extension}`;
        if (zipped) {
            const files = successes.map((item) => ({
                name: `sample-${item.index + 1}.${job.format}`,
                data: render(job, [item]),
            }));
            job.file = packZip(files);
        } else {
            job.file = render(job, successes);
        }
        usedSeconds += duration * successes.length;
        job.result = {
            filename, mime: mime[extension], size: job.file.length, download_url: `/api/jobs/${job.id}/download`,
        };
    }

    function create(form) {
        if (active) return fail("JOB_ACTIVE", 409);
        const urls = JSON.parse(form.get("urls") || "[]");
        const files = form.getAll("files");
        const format = form.get("format") || "txt";
        if (urls.some((url) => url.toLowerCase().includes("quota"))) {
            return fail("QUOTA_EXCEEDED", 429, { remaining_min: 0, resets_in_s: resets });
        }
        if (!config.formats.includes(format)) return fail("BAD_FORMAT", 400);
        const sources = urls.concat(files.map((file) => file.name));
        if (!sources.length) return fail("NO_INPUT", 400);
        if (sources.length > config.max_items) return fail("BATCH_TOO_LARGE", 400, { max_items: config.max_items });
        if (files.some((file) => file.size > config.max_upload_mb * 1024 * 1024)) {
            return fail("TOO_LARGE", 413, { max_mb: config.max_upload_mb });
        }
        if (usedSeconds + duration * sources.length > 120 * 60) {
            return fail("QUOTA_EXCEEDED", 429, { remaining_min: quota().remaining_min, resets_in_s: resets });
        }

        const id = `mock-job-${++sequence}`;
        const items = sources.map((source, index) => ({
            index, source, title: null, duration_s: null, status: "pending", error: null,
        }));
        const failed = new Set();
        urls.forEach((url, index) => {
            if (url.toLowerCase().includes("unavailable")) failed.add(index);
        });
        jobs.set(id, {
            id, items, failed, format, phase: 0, completed: 0, error: null, result: null, file: null,
            cancelled: false,
            combine: form.get("combine") === "true" && config.combinable.includes(format),
            timestamps: form.get("timestamps") === "true",
            language: form.get("language"),
        });
        active = id;
        return json({ job_id: id, items }, 202);
    }

    function snapshot(job) {
        const phase = job.phase;
        if (!job.cancelled && phase === phases.length - 1 && !job.completed) finish(job);
        const status = job.cancelled ? "cancelled" : job.error ? "error" : phases[phase];
        const items = job.items.map((item) => {
            const unavailable = phase >= 2 && job.failed.has(item.index);
            const itemStatus = ["pending", "pending", "downloading", "transcribing", "done", "done"][phase];
            return {
                ...item,
                duration_s: phase > 1 && !unavailable ? duration : null,
                status: unavailable || job.cancelled ? "error" : itemStatus,
                error: unavailable ? error("UNAVAILABLE") : job.cancelled ? error("CANCELLED") : null,
            };
        });
        if (!job.cancelled && phase < phases.length - 1) job.phase += 1;
        return json({
            status, progress: progress[phase], queue_position: status === "queued" ? 1 : 0,
            items, result: job.result, error: job.cancelled ? error("CANCELLED") : job.error, quota: quota(),
        });
    }

    return async function mockApi(path, options = {}) {
        options.signal?.throwIfAborted();
        const method = options.method || "GET";
        if (path === "/api/health" && method === "GET") return json({ ok: true, version: "1" });
        if (path === "/api/config" && method === "GET") return json(config);
        if (path === "/api/quota" && method === "GET") return json(quota());
        if (path === "/api/jobs" && method === "POST") return create(options.body);

        const route = /^\/api\/jobs\/(mock-job-\d+)(\/download)?$/.exec(path);
        const job = route && jobs.get(route[1]);
        if (!job) return fail("NOT_FOUND", 404);
        if (route[2] && method === "GET") {
            if (!job.result) return fail("NOT_FOUND", 404);
            if (Date.now() - job.completed >= 3600000) return fail("RESULT_EXPIRED", 404);
            return new Response(job.file, { headers: {
                "Content-Type": job.result.mime,
                "Content-Disposition": `attachment; filename="${job.result.filename}"`,
            } });
        }
        if (!route[2] && method === "DELETE") {
            if (active === job.id) {
                job.cancelled = true;
                active = null;
            }
            return json({ ok: true });
        }
        if (!route[2] && method === "GET") return snapshot(job);
        return fail("NOT_FOUND", 404);
    };
}
})();
