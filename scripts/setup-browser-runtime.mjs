import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { copyFile, mkdir, mkdtemp, realpath, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

const run = promisify(execFile);
const target = fileURLToPath(new URL("../static/assets/vendor/", import.meta.url));
const packages = [
  {
    url: "https://registry.npmjs.org/@huggingface/transformers/-/transformers-4.3.2.tgz",
    sha512: "V3+dqf3iWlRYJ9I9GreM3zGQd0lAXnnoPeddmkOQYWAEE4l/+Fi6p7tx/qVUJlKQgLXGim2W76rTOzz+eC0i7g==",
    files: {
      "dist/transformers.min.js": "transformers.min.js",
      LICENSE: "Transformers-LICENSE.txt",
    },
  },
  {
    // This is the precise ONNX Runtime Web release Transformers.js 4.3.2 was built against.
    url: "https://registry.npmjs.org/onnxruntime-web/-/onnxruntime-web-1.31.0-dev.20260914-8d85527a0.tgz",
    sha512: "Iy7rtadoBgxS/LLvDr3QW38DB1PNXRnr0GJMcL0TAt7c9qjgVQl83UlGCVyeAnK2InpmW8Uc3PL8XIuqtDeF6g==",
    files: {
      "dist/ort-wasm-simd-threaded.asyncify.mjs": "ort-wasm-simd-threaded.asyncify.mjs",
      "dist/ort-wasm-simd-threaded.asyncify.wasm": "ort-wasm-simd-threaded.asyncify.wasm",
      "dist/ort-wasm-simd-threaded.mjs": "ort-wasm-simd-threaded.mjs",
      "dist/ort-wasm-simd-threaded.wasm": "ort-wasm-simd-threaded.wasm",
    },
  },
];

// ONNX Runtime Web's npm archive does not include the project's MIT license.
const ortLicense = `MIT License

Copyright (c) Microsoft Corporation

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

Project and third-party notices:
https://github.com/microsoft/onnxruntime/blob/main/ThirdPartyNotices.txt
`;

async function extract(spec, workspace) {
  const name = basename(new URL(spec.url).pathname);
  const response = await fetch(spec.url, { redirect: "error", signal: AbortSignal.timeout(120_000) });
  if (!response.ok) throw new Error(`Download failed for ${name} (${response.status}).`);

  const data = Buffer.from(await response.arrayBuffer());
  if (createHash("sha512").update(data).digest("base64") !== spec.sha512) {
    throw new Error(`Checksum mismatch for ${name}.`);
  }

  const archive = join(workspace, name);
  await writeFile(archive, data);
  const files = Object.keys(spec.files).map((path) => `package/${path}`);
  await run(process.platform === "win32" ? "tar.exe" : "tar", ["-xf", archive, "-C", workspace, ...files]);
}

async function install() {
  const temporaryRoot = await realpath(tmpdir());
  const temporary = await mkdtemp(join(temporaryRoot, "transcriptor-browser-"));

  try {
    for (const spec of packages) await extract(spec, temporary);

    await mkdir(target, { recursive: true });
    for (const spec of packages) {
      for (const [source, filename] of Object.entries(spec.files)) {
        await copyFile(join(temporary, "package", source), join(target, filename));
      }
    }
    await writeFile(join(target, "ONNXRuntime-LICENSE.txt"), ortLicense);
    console.log("Browser runtime ready in static/assets/vendor/.");
  } finally {
    const resolved = await realpath(temporary);
    if (dirname(resolved) === temporaryRoot && basename(resolved).startsWith("transcriptor-browser-")) {
      await rm(resolved, { recursive: true, force: true });
    }
  }
}

try {
  await install();
} catch (err) {
  console.error(`Browser runtime setup failed: ${err.message}`);
  process.exitCode = 1;
}
