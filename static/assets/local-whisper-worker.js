const base = { model: "onnx-community/whisper-base", revision: "27a07b4b52b3a4c5886134bb4320e93e1a4bbc7c" };
const tiny = { model: "onnx-community/whisper-tiny", revision: "ff4177021cc41f7db950912b73ea4fdf7d01d8e7" };
const languages = new Set(["en", "es", "pt"]);
const sampleRate = 16000;
const chunkSeconds = 30;
const overlapSeconds = 5;

let session;
let busy = false;

function send(stage, details = {}) {
  self.postMessage({ stage, ...details });
}

function reportLoading(engine) {
  const weights = new Map();
  const downloaded = new Set();
  let lastProgress = 0;
  return ({ file, status, loaded, total }) => {
    if (!file?.endsWith(".onnx") || downloaded.size === 2) return;
    if (status === "done") {
      downloaded.add(file);
      if (downloaded.size === 2) send("initializing", { engine });
      return;
    }
    // Ignore small tokenizer and config files for the progress estimate.
    if (status !== "progress"
      || !Number.isFinite(total) || total <= 0 || !Number.isFinite(loaded)) return;
    weights.set(file, { loaded, total });
    if (weights.size < 2) return;
    const all = Array.from(weights.values());
    const progress = Math.min(1, all.reduce((sum, item) => sum + item.loaded, 0)
      / all.reduce((sum, item) => sum + item.total, 0));
    if (progress - lastProgress >= 0.01 || progress === 1) {
      lastProgress = progress;
      if (progress === 1) send("initializing", { engine });
      else send("loading", { engine, progress });
    }
  };
}

function configureRuntime(runtime, engine, compact) {
  const { env } = runtime;
  env.allowLocalModels = false;
  // Let the browser stream/HTTP-cache the same-origin WASM instead of preloading a second copy.
  if (compact) env.useWasmCache = false;
  if (runtime.LogLevel) env.logLevel = runtime.LogLevel.ERROR;

  const wasm = env.backends.onnx.wasm;
  if (wasm) {
    // Safari versions before 26 lack the asyncify support needed by this ORT build.
    const safariVersion = navigator.userAgent.match(/Version\/(\d+)(?:\.\d+)*.* Safari\//);
    const plainWasm = engine === "wasm" && (compact || (safariVersion && Number(safariVersion[1]) < 26));
    const build = plainWasm ? "" : ".asyncify";
    wasm.wasmPaths = {
      mjs: new URL(`./vendor/ort-wasm-simd-threaded${build}.mjs`, import.meta.url).href,
      wasm: new URL(`./vendor/ort-wasm-simd-threaded${build}.wasm`, import.meta.url).href,
    };
    // The worker already keeps inference off the UI thread.
    wasm.proxy = false;
    if (compact || !self.crossOriginIsolated) wasm.numThreads = 1;
  }
}

async function supportsWebGpu() {
  try {
    return Boolean(await navigator.gpu?.requestAdapter());
  } catch {
    return false;
  }
}

async function getTranscriber(runtime, engine, compact) {
  if (session?.engine === engine) return session.transcribe;
  const previous = session;
  session = undefined;
  if (previous) await previous.transcribe.dispose();

  configureRuntime(runtime, engine, compact);
  const { model, revision } = compact ? tiny : base;
  const transcribe = await runtime.pipeline("automatic-speech-recognition", model, {
    device: engine,
    revision,
    // The WebGPU demo uses a full precision encoder and a four bit decoder.
    dtype: engine === "webgpu" ? { encoder_model: "fp32", decoder_model_merged: "q4" } : "q8",
    ...(compact && { session_options: { enableCpuMemArena: false, enableMemPattern: false } }),
    progress_callback: reportLoading(engine),
  });
  session = { engine, transcribe };
  return transcribe;
}

async function detectLanguage(runtime, transcribe, audio) {
  // This runtime substitutes English when language is omitted. Whisper's first
  // decoder step, before a task or language token, scores the spoken languages.
  const { model, processor } = transcribe;
  const config = model.generation_config;
  const start = config.decoder_start_token_id ?? model.config.decoder_start_token_id;
  const inputs = await processor(audio.subarray(0, chunkSeconds * sampleRate));
  const decoderStart = new runtime.Tensor("int64", BigInt64Array.of(BigInt(start)), [1, 1]);
  let output;
  try {
    output = await model({ input_features: inputs.input_features, decoder_input_ids: decoderStart });
    const scores = output.logits.data;
    let bestToken;
    let bestScore = -Infinity;
    for (const [token, id] of Object.entries(config.lang_to_id)) {
      if (scores[id] > bestScore) {
        bestScore = scores[id];
        bestToken = token;
      }
    }
    if (!bestToken) throw new Error("No language score available");
    return bestToken.slice(2, -2);
  } finally {
    for (const tensor of Object.values(output ?? {})) tensor.dispose?.();
    for (const tensor of Object.values(inputs)) tensor.dispose?.();
    decoderStart.dispose();
  }
}

async function transcribeAudio({ audio, language, timestamps, device, compact, prepare }) {
  let runtime;
  try {
    if (!prepare && (!(audio instanceof Float32Array) || !audio.length)) {
      send("error", { reason: "audio" });
      return;
    }
    runtime = await import("./vendor/transformers.min.js");
  } catch {
    send("error", { reason: "runtime" });
    return;
  }

  const engine = compact || device === "wasm" ? "wasm" : session?.engine ?? (await supportsWebGpu() ? "webgpu" : "wasm");
  try {
    if (session?.engine !== engine) send("loading", { engine });
    const transcribe = await getTranscriber(runtime, engine, compact);
    if (prepare) {
      send("ready");
      return;
    }
    const step = chunkSeconds - 2 * overlapSeconds;
    const total = 1 + Math.max(0, Math.ceil((audio.length - chunkSeconds * sampleRate) / (step * sampleRate)));
    let completed = 0;
    // The pipeline decodes the final text itself. Count completed windows without
    // decoding each intermediate token again just to update the progress bar.
    const streamer = {
      put() {},
      end() {
        completed = Math.min(completed + 1, total);
        send("transcribing", { engine, completed, total });
      },
    };
    send("transcribing", { engine, completed, total });
    const spokenLanguage = languages.has(language) ? language : await detectLanguage(runtime, transcribe, audio);
    const output = await transcribe(audio, {
      task: "transcribe",
      language: spokenLanguage,
      chunk_length_s: chunkSeconds,
      stride_length_s: overlapSeconds,
      return_timestamps: Boolean(timestamps),
      streamer,
    });
    const text = output.text.trim();
    const segments = (output.chunks ?? []).map(({ text, timestamp }) => ({
      text: text.trim(), start: timestamp?.[0] ?? null, end: timestamp?.[1] ?? null,
    })).filter((segment) => segment.text);
    send(text || segments.length ? "done" : "error", text || segments.length
      ? { text, segments, engine } : { reason: "empty" });
  } catch {
    if (engine === "webgpu") {
      // Retry with a fresh runtime because a failed ONNX session can block later calls.
      self.postMessage({ stage: "fallback", audio }, [audio.buffer]);
    } else {
      send("error", { reason: "inference" });
    }
  }
}

self.addEventListener("message", async ({ data }) => {
  if (busy || !data) return;
  busy = true;
  try {
    await transcribeAudio(data);
  } finally {
    busy = false;
  }
});
