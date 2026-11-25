import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import test from "node:test";

// Run the published worker protocol without downloading weights in the test suite.
async function workerFixture(t) {
  const messages = [];
  const calls = [];
  const inference = [];
  const env = { backends: { onnx: { wasm: {} } } };
  let receive;
  const runtime = {
    env,
    Tensor: class { dispose() {} },
    async pipeline(task, model, options) {
      calls.push({ task, model, options });
      for (const file of ["encoder_model_quantized.onnx", "decoder_model_merged_quantized.onnx"]) {
        options.progress_callback({ status: "done", file });
      }
      const transcribe = async (_audio, settings) => {
        inference.push(settings);
        settings.streamer.end();
        return { text: "Hola.", chunks: [{ timestamp: [0, 1], text: "Hola." }] };
      };
      transcribe.processor = async () => ({ input_features: {} });
      transcribe.model = async () => ({ logits: { data: [99, 1, 5, -2] } });
      transcribe.model.generation_config = {
        decoder_start_token_id: 4, lang_to_id: { "<|en|>": 1, "<|es|>": 2, "<|pt|>": 3 },
      };
      return transcribe;
    },
  };
  globalThis.testRuntime = runtime;
  globalThis.self = {
    crossOriginIsolated: true,
    postMessage: (message) => messages.push(message),
    addEventListener: (_event, handler) => { receive = handler; },
  };
  const hooks = registerHooks({
    resolve(specifier, context, next) {
      if (specifier.endsWith("/vendor/transformers.min.js")) {
        const url = "data:text/javascript,export const {env,pipeline,Tensor}=globalThis.testRuntime#" + encodeURIComponent(t.name);
        return { url, shortCircuit: true };
      }
      return next(specifier, context);
    },
  });
  t.after(() => { hooks.deregister(); delete globalThis.self; delete globalThis.testRuntime; });
  // Give each test an independent worker and stub module.
  const url = new URL("../static/assets/local-whisper-worker.js", import.meta.url);
  url.searchParams.set("test", t.name);
  await import(url);
  return { send: (data) => receive({ data }), messages, calls, inference, env };
}

test("mobile loads Tiny on plain single-thread WASM before receiving audio", async (t) => {
  const w = await workerFixture(t);
  await w.send({ prepare: true, compact: true, device: "wasm" });
  assert.equal(w.calls[0].model, "onnx-community/whisper-tiny");
  assert.equal(w.calls[0].options.dtype, "q8");
  assert.equal(w.calls[0].options.device, "wasm");
  assert.equal(w.env.backends.onnx.wasm.numThreads, 1);
  assert.match(w.env.backends.onnx.wasm.wasmPaths.wasm, /threaded\.wasm$/);
  assert.equal(w.env.useWasmCache, false);
  assert.deepEqual(w.messages.map((m) => m.stage), ["loading", "initializing", "ready"]);
  await w.send({ compact: true, audio: new Float32Array(16000), language: "es", timestamps: true });
  assert.equal(w.calls.length, 1);
  assert.equal(w.messages.at(-1).stage, "done");
  assert.deepEqual(w.messages.at(-1).segments, [{ text: "Hola.", start: 0, end: 1 }]);
});

test("desktop accepts the existing one-message Base workflow", async (t) => {
  const w = await workerFixture(t);
  await w.send({ audio: new Float32Array(16000), language: "es", device: "wasm" });
  assert.equal(w.calls[0].model, "onnx-community/whisper-base");
  assert.equal(w.messages.at(-1).stage, "done");
  assert.ok(!w.messages.some((m) => m.stage === "ready"));
});

test("auto selects the strongest language token and transcribes in that language", async (t) => {
  const w = await workerFixture(t);
  const audio = new Float32Array(16000);
  await w.send({ audio, language: "auto", device: "wasm" });
  await w.send({ audio, language: "pt", device: "wasm" });
  assert.deepEqual(w.inference.map(({ language, task }) => [language, task]),
    [["es", "transcribe"], ["pt", "transcribe"]]);
});
